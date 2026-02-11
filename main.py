"""
main.py - Hoofdscript voor automatische padelbaan reservering bij TPV Heksenwiel.

Gebruik:
    python main.py                  # Automatisch: bepaal dag op basis van huidige tijd
    python main.py --visible        # Met zichtbare browser (voor testen)
    python main.py --dag 1          # Reserveer voor specifieke dag (0=ma..6=zo)
    python main.py --dry-run        # Simuleer zonder daadwerkelijk te reserveren
    python main.py --no-retry       # Eenmalige poging (geen retry-loop)
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from browser_bot import ReserveringBot, ReserveringError
from notifier import EmailNotifier

# Pad naar dit script
BASE_DIR = Path(__file__).parent

# Logging configuratie
LOG_FILE = BASE_DIR / "reservering.log"

DAGNAMEN = {
    0: "maandag",
    1: "dinsdag",
    2: "woensdag",
    3: "donderdag",
    4: "vrijdag",
    5: "zaterdag",
    6: "zondag",
}

# Retry configuratie
RETRY_INTERVAL_SEC = 10     # Seconden tussen pogingen
RETRY_TIMEOUT_MIN = 7       # Maximaal aantal minuten retrying (5 min voor + 2 min na)
VOORBEREIDING_MIN = 5       # Minuten voor de 48u-grens dat we starten met voorbereiden


def setup_logging(verbose: bool = False):
    """Configureer logging naar bestand en console."""
    log_level = logging.DEBUG if verbose else logging.INFO

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Bestandshandler
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def load_config() -> dict:
    """Laad de configuratie uit config.yaml."""
    config_path = BASE_DIR / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Configuratiebestand niet gevonden: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return config


def bepaal_automatische_dag(config: dict) -> dict | None:
    """
    Bepaal automatisch welke dag gereserveerd moet worden op basis van het
    huidige tijdstip en de 48-uur reserveringsregel.

    Logica: Het script draait ~5 minuten voor de 48u-grens.
    Als het nu zondag 20:25 is, dan is 48u later = dinsdag 20:25.
    Dat betekent dat we dinsdag 20:30 willen reserveren.

    Returns:
        De dag_config dict die nu aan de beurt is, of None.
    """
    logger = logging.getLogger(__name__)
    nu = datetime.now()
    uren_vooruit = config.get("reservering", {}).get("uren_vooruit", 48)
    dagen_config = config.get("reservering", {}).get("dagen", [])

    logger.info(f"Automatische dagbepaling - huidig tijdstip: {nu.strftime('%A %d-%m-%Y %H:%M')}")

    # Bereken het tijdstip dat over ~48 uur valt (met wat marge)
    # We nemen aan dat dit script ~5 min voor de 48u-grens draait
    target_moment = nu + timedelta(hours=uren_vooruit, minutes=VOORBEREIDING_MIN)
    target_weekdag = target_moment.weekday()  # 0=ma, 6=zo

    logger.info(f"Doeltijdstip (nu + {uren_vooruit}u + {VOORBEREIDING_MIN}m): "
                f"{target_moment.strftime('%A %d-%m-%Y %H:%M')} (weekdag={target_weekdag})")

    for dag_config in dagen_config:
        if dag_config["dag"] == target_weekdag:
            # Controleer of de eerste voorkeurstijd in de buurt ligt
            eerste_tijd = dag_config.get("tijden", ["19:00"])[0]
            try:
                uur, minuut = map(int, eerste_tijd.split(":"))
                reserveer_moment = target_moment.replace(hour=uur, minute=minuut, second=0)
                verschil_min = abs((target_moment - reserveer_moment).total_seconds() / 60)

                # Als het verschil minder dan 30 minuten is, is dit de juiste dag
                if verschil_min <= 30:
                    logger.info(f"Match gevonden: {DAGNAMEN[dag_config['dag']]} {eerste_tijd} "
                                f"(verschil: {verschil_min:.0f} min)")
                    return dag_config
                else:
                    logger.debug(f"Dag {DAGNAMEN[dag_config['dag']]} {eerste_tijd} - "
                                 f"verschil te groot: {verschil_min:.0f} min")
            except ValueError:
                pass

    # Fallback: probeer de dag die het dichtst bij het target moment ligt
    logger.warning("Geen exacte match gevonden, probeer dichtstbijzijnde dag...")
    for dag_config in dagen_config:
        if dag_config["dag"] == target_weekdag:
            logger.info(f"Fallback match: {DAGNAMEN[dag_config['dag']]}")
            return dag_config

    logger.warning("Geen passende dag gevonden voor automatische reservering")
    return None


def bereken_target_datum(dag_config: dict, uren_vooruit: int) -> datetime | None:
    """
    Bereken de eerstvolgende datum voor een gewenste dag.

    Returns:
        De target datum, of None als buiten de reserveringsperiode.
    """
    nu = datetime.now()
    vandaag = nu.replace(hour=0, minute=0, second=0, microsecond=0)
    gewenste_dag = dag_config["dag"]
    huidige_dag = vandaag.weekday()

    dagen_tot = (gewenste_dag - huidige_dag) % 7
    if dagen_tot == 0:
        # Vandaag: controleer of het tijdstip nog in de toekomst is
        eerste_tijd = dag_config.get("tijden", ["19:00"])[0]
        try:
            uur, minuut = map(int, eerste_tijd.split(":"))
            if nu.hour > uur or (nu.hour == uur and nu.minute >= minuut):
                dagen_tot = 7  # Vandaag is al geweest, pak volgende week
        except ValueError:
            dagen_tot = 7

    target = vandaag + timedelta(days=dagen_tot)

    # Controleer of het binnen de reserveringsperiode valt
    eerste_tijd = dag_config.get("tijden", ["19:00"])[0]
    try:
        uur, minuut = map(int, eerste_tijd.split(":"))
        target_met_tijd = target.replace(hour=uur, minute=minuut)
    except (ValueError, IndexError):
        target_met_tijd = target.replace(hour=19, minute=0)

    # Iets ruimere controle: we starten 5 min voor de grens
    max_tijdstip = nu + timedelta(hours=uren_vooruit, minutes=VOORBEREIDING_MIN + 5)
    if target_met_tijd > max_tijdstip:
        return None

    return target


def get_spelers(config: dict, dag: int) -> list[str]:
    """Haal de lijst met medespelers op voor een specifieke dag."""
    medespelers_config = config.get("medespelers", {})
    spelers_per_dag = medespelers_config.get("spelers_per_dag", {})
    if spelers_per_dag and dag in spelers_per_dag:
        return spelers_per_dag[dag]
    return medespelers_config.get("standaard_spelers", [])


def wacht_tot_48u_grens(target_date: datetime, eerste_tijd: str, uren_vooruit: int):
    """
    Wacht tot het 48u-reserveringsvenster bijna open is.

    Het script is al gestart en voorbereid (login + stap 1+2 klaar).
    Nu wachten we tot kort voor de 48u-grens zodat we meteen kunnen proberen.
    """
    logger = logging.getLogger(__name__)

    try:
        uur, minuut = map(int, eerste_tijd.split(":"))
    except ValueError:
        uur, minuut = 19, 0

    # Het moment waarop het reserveringsvenster opengaat
    reservering_dt = target_date.replace(hour=uur, minute=minuut, second=0, microsecond=0)
    venster_open = reservering_dt - timedelta(hours=uren_vooruit)

    nu = datetime.now()
    wachttijd = (venster_open - nu).total_seconds()

    if wachttijd > 0:
        logger.info(f"Reserveringsvenster opent om: {venster_open.strftime('%H:%M:%S')}")
        logger.info(f"Nog {wachttijd:.0f} seconden wachten ({wachttijd/60:.1f} minuten)...")

        # Wacht in stappen van 10 seconden zodat we logs zien
        while wachttijd > 0:
            slaap = min(wachttijd, 10)
            time.sleep(slaap)
            nu = datetime.now()
            wachttijd = (venster_open - nu).total_seconds()
            if wachttijd > 0 and int(wachttijd) % 60 < 11:
                logger.info(f"Nog {wachttijd:.0f}s tot venster opent...")
    else:
        logger.info(f"Reserveringsvenster is al open (sinds {abs(wachttijd):.0f}s geleden)")


def reserveer_met_retry(config: dict, dag_config: dict, dry_run: bool = False) -> dict:
    """
    Voer een reservering uit met retry-loop.

    Strategie:
    1. Start browser, login, voer stap 1+2 uit (voorbereiding)
    2. Wacht tot vlak voor de 48u-grens
    3. Probeer elke 10 seconden stap 3+4 uit te voeren
    4. Stop bij: succes, alle banen bezet, of timeout (7 min)
    """
    logger = logging.getLogger(__name__)
    dag = dag_config["dag"]
    tijden = dag_config.get("tijden", ["19:00"])
    uren_vooruit = config.get("reservering", {}).get("uren_vooruit", 48)
    baan_voorkeur = config.get("reservering", {}).get("baan_voorkeur", [])

    target_date = bereken_target_datum(dag_config, uren_vooruit)
    if target_date is None:
        msg = f"Dag {DAGNAMEN.get(dag, dag)} valt buiten de reserveringsperiode"
        logger.warning(msg)
        return {
            "success": False, "datum": "n.v.t.", "tijd": None,
            "baan": None, "spelers": [], "foutmelding": msg,
        }

    dagnaam = DAGNAMEN.get(dag, str(dag))
    spelers = get_spelers(config, dag)

    logger.info("=" * 60)
    logger.info(f"RESERVERING MET RETRY - {dagnaam} {target_date.strftime('%d-%m-%Y')}")
    logger.info(f"Tijden: {tijden} | Spelers: {spelers}")
    logger.info("=" * 60)

    result = {
        "success": False,
        "datum": target_date.strftime("%d-%m-%Y"),
        "tijd": None,
        "baan": None,
        "spelers": spelers,
        "foutmelding": None,
    }

    bot = ReserveringBot(config)
    try:
        # --- FASE 1: VOORBEREIDING (voor de 48u-grens) ---
        logger.info("--- FASE 1: Voorbereiding (login + stap 1 + stap 2) ---")
        bot.start()

        fout = bot.voorbereiden(target_date, tijden, spelers)
        if fout:
            result["foutmelding"] = fout
            logger.error(f"Voorbereiding mislukt: {fout}")
            return result

        logger.info("Voorbereiding gelukt! Klaar op stap 3.")

        # --- FASE 2: WACHT OP 48U-GRENS ---
        logger.info("--- FASE 2: Wachten op reserveringsvenster ---")
        eerste_tijd = tijden[0]
        wacht_tot_48u_grens(target_date, eerste_tijd, uren_vooruit)

        # --- FASE 3: RETRY-LOOP ---
        logger.info("--- FASE 3: Retry-loop gestart ---")
        start_retry = datetime.now()
        timeout = timedelta(minutes=RETRY_TIMEOUT_MIN)
        poging_nr = 0

        while datetime.now() - start_retry < timeout:
            poging_nr += 1
            logger.info(f"--- Poging {poging_nr} ({datetime.now().strftime('%H:%M:%S')}) ---")

            poging = bot.probeer_reserveer(tijden, baan_voorkeur, dry_run)

            if poging["success"]:
                result["success"] = True
                result["tijd"] = poging["tijd"]
                result["baan"] = poging["baan"]
                result["foutmelding"] = poging.get("foutmelding")
                logger.info(f"SUCCES na {poging_nr} poging(en): "
                            f"{poging['tijd']} op {poging['baan']}")
                return result

            if not poging["retry"]:
                # Definitief mislukt (bijv. baan al bezet, geen retry mogelijk)
                result["foutmelding"] = poging["foutmelding"]
                logger.warning(f"Definitief mislukt na {poging_nr} pogingen: "
                               f"{poging['foutmelding']}")
                return result

            # Retry - wacht even en probeer opnieuw
            logger.info(f"Nog niet gelukt ({poging['foutmelding']}), "
                        f"volgende poging over {RETRY_INTERVAL_SEC}s...")
            time.sleep(RETRY_INTERVAL_SEC)

        # Timeout bereikt
        result["foutmelding"] = (
            f"Timeout na {RETRY_TIMEOUT_MIN} minuten en {poging_nr} pogingen"
        )
        logger.error(result["foutmelding"])

    except Exception as e:
        result["foutmelding"] = f"Onverwachte fout: {e}"
        logger.error(f"Onverwachte fout: {e}", exc_info=True)
    finally:
        bot.stop()

    return result


def reserveer_voor_dag(config: dict, dag_config: dict, dry_run: bool = False) -> dict:
    """
    Voer een reservering uit voor een specifieke dag (zonder retry, voor lokaal testen).
    """
    logger = logging.getLogger(__name__)
    dag = dag_config["dag"]
    tijden = dag_config.get("tijden", ["19:00"])
    uren_vooruit = config.get("reservering", {}).get("uren_vooruit", 48)
    baan_voorkeur = config.get("reservering", {}).get("baan_voorkeur", [])

    target_date = bereken_target_datum(dag_config, uren_vooruit)
    if target_date is None:
        msg = f"Dag {DAGNAMEN.get(dag, dag)} valt buiten de reserveringsperiode ({uren_vooruit} uur vooruit)"
        logger.warning(msg)
        return {
            "success": False, "datum": "n.v.t.", "tijd": None,
            "baan": None, "spelers": [], "foutmelding": msg,
        }

    dagnaam = DAGNAMEN.get(dag, str(dag))
    spelers = get_spelers(config, dag)
    logger.info(f"=== Reservering voor {dagnaam} {target_date.strftime('%d-%m-%Y')} ===")
    logger.info(f"Voorkeurtijden: {tijden} | Medespelers: {spelers}")

    bot = ReserveringBot(config)
    try:
        bot.start()
        result = bot.reserveer(
            target_date=target_date,
            tijden=tijden,
            spelers=spelers,
            baan_voorkeur=baan_voorkeur,
            dry_run=dry_run,
        )
    finally:
        bot.stop()

    return result


def main():
    """Hoofdfunctie."""
    parser = argparse.ArgumentParser(
        description="Automatische padelbaan reservering - TPV Heksenwiel"
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Open de browser zichtbaar (niet headless) voor testen",
    )
    parser.add_argument(
        "--dag",
        type=int,
        choices=range(7),
        help="Reserveer alleen voor specifieke dag (0=ma, 1=di, ..., 6=zo)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simuleer het proces zonder daadwerkelijk te reserveren",
    )
    parser.add_argument(
        "--no-retry",
        action="store_true",
        help="Eenmalige poging zonder retry-loop (voor lokaal testen)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Toon extra debug informatie",
    )
    args = parser.parse_args()

    # Setup
    setup_logging(verbose=args.verbose)
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Padel Reservering Bot - TPV Heksenwiel")
    logger.info(f"Gestart op: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Modus: {'dry-run' if args.dry_run else 'live'} | "
                f"Retry: {'nee' if args.no_retry else 'ja'}")
    logger.info("=" * 60)

    # Laad configuratie
    try:
        config = load_config()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except yaml.YAMLError as e:
        logger.error(f"Fout in config.yaml: {e}")
        sys.exit(1)

    # Override headless als --visible is meegegeven
    if args.visible:
        config.setdefault("browser", {})["headless"] = False
        config["browser"]["slow_mo"] = max(config["browser"].get("slow_mo", 0), 300)

    # Bepaal welke dag(en) we moeten reserveren
    if args.dag is not None:
        # Specifieke dag opgegeven via command line
        dagen_config = config.get("reservering", {}).get("dagen", [])
        dag_config_lijst = [d for d in dagen_config if d["dag"] == args.dag]
        if not dag_config_lijst:
            logger.error(f"Dag {args.dag} ({DAGNAMEN.get(args.dag, '?')}) "
                         f"niet geconfigureerd in config.yaml")
            sys.exit(1)
        dag_config = dag_config_lijst[0]
    else:
        # Automatisch bepalen welke dag aan de beurt is
        dag_config = bepaal_automatische_dag(config)
        if dag_config is None:
            logger.error("Kon niet automatisch bepalen welke dag gereserveerd moet worden. "
                         "Gebruik --dag <nummer> om de dag handmatig op te geven.")
            sys.exit(1)

    dagnaam = DAGNAMEN.get(dag_config["dag"], str(dag_config["dag"]))
    logger.info(f"Reservering voor: {dagnaam}")

    # E-mail notifier
    email_config = config.get("email", {})
    notifier = EmailNotifier(email_config)

    # Voer de reservering uit
    if args.no_retry:
        result = reserveer_voor_dag(config, dag_config, dry_run=args.dry_run)
    else:
        result = reserveer_met_retry(config, dag_config, dry_run=args.dry_run)

    # Verstuur notificatie
    if not args.dry_run:
        notifier.verstuur(result)
    else:
        logger.info("DRY RUN - Geen e-mail verstuurd")

    # Log resultaat
    if result["success"]:
        logger.info(f"SUCCES: {dagnaam} {result['datum']} om {result['tijd']} "
                     f"op baan {result['baan']}")
    else:
        logger.warning(f"MISLUKT: {dagnaam} - {result.get('foutmelding', 'onbekende fout')}")

    # Samenvatting
    logger.info("=" * 60)
    status = "GELUKT" if result["success"] else "MISLUKT"
    logger.info(f"RESULTAAT: {status}")
    logger.info(f"  Datum: {result['datum']}")
    logger.info(f"  Tijd: {result.get('tijd', '-')}")
    logger.info(f"  Baan: {result.get('baan', '-')}")
    logger.info(f"  Spelers: {result.get('spelers', [])}")
    if result.get("foutmelding"):
        logger.info(f"  Melding: {result['foutmelding']}")
    logger.info("=" * 60)

    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()

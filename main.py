"""
main.py - Hoofdscript voor automatische padelbaan reservering.

Gebruik:
    python main.py                  # Automatisch: bepaal dag op basis van huidige tijd
    python main.py --dag 1          # Reserveer voor specifieke dag (0=ma..6=zo)
    python main.py --dry-run        # Simuleer zonder daadwerkelijk te reserveren
    python main.py --no-retry       # Eenmalige poging (geen retry-loop)
    python main.py --sync-spelers   # Haal spelerslijst op van KNLTB en sla op als players.json
"""

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from api_bot import ApiReserveringBot, ReserveringError
from notifier import EmailNotifier

# Pad naar dit script
BASE_DIR = Path(__file__).parent

# Tijdzone: config-tijden zijn Nederlandse lokale tijd
NL_TZ = ZoneInfo("Europe/Amsterdam")

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

# Timing configuratie
RETRY_INTERVAL_SEC = 10     # Seconden tussen pogingen
VOORBEREIDING_MIN = 3       # Minuten voor de 48u-grens dat we inloggen en spelers selecteren
NA_VENSTER_MAX_MIN = 15     # Minuten na het 48u-venster dat we blijven proberen


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


def vind_reserveerbare_dagen(config: dict) -> list[dict]:
    """
    Vind alle geconfigureerde dagen waarvan de eerstvolgende datum
    binnen het 48-uur reserveringsvenster valt.

    Werkt zowel voor geplande cron-runs als handmatige triggers:
    doorloopt alle dagen uit de config en controleert of ze nu
    reserveerbaar zijn.

    Returns:
        Lijst van dag_config dicts die nu reserveerbaar zijn (kan leeg zijn).
    """
    logger = logging.getLogger(__name__)
    nu = datetime.now(NL_TZ)
    uren_vooruit = config.get("reservering", {}).get("uren_vooruit", 48)
    dagen_config = config.get("reservering", {}).get("dagen", [])
    reserveerbaar = []

    logger.info(f"Zoek reserveerbare dagen - huidig tijdstip: {nu.strftime('%A %d-%m-%Y %H:%M %Z')}")

    for dag_config in dagen_config:
        dag = dag_config["dag"]
        dagnaam = DAGNAMEN.get(dag, str(dag))
        target = bereken_target_datum(dag_config, uren_vooruit)

        if target is not None:
            eerste_tijd = dag_config.get("tijden", ["19:00"])[0]
            logger.info(f"  {dagnaam} {target.strftime('%d-%m-%Y')} {eerste_tijd} -> RESERVEERBAAR")
            reserveerbaar.append(dag_config)
        else:
            logger.debug(f"  {dagnaam} -> buiten 48u venster")

    logger.info(f"Gevonden: {len(reserveerbaar)} reserveerbare dag(en)")
    return reserveerbaar


def bereken_target_datum(dag_config: dict, uren_vooruit: int) -> date | None:
    """
    Bereken de eerstvolgende datum voor een gewenste dag.

    Geeft een date-object terug (geen datetime) om DST-problemen met
    datetime.replace() te voorkomen. Timezone-aware datetimes worden
    pas later geconstrueerd via datetime(..., tzinfo=NL_TZ).

    Returns:
        De target datum als date, of None als buiten de reserveringsperiode.
    """
    nu = datetime.now(NL_TZ)
    vandaag_date = nu.date()
    gewenste_dag = dag_config["dag"]
    huidige_dag = vandaag_date.weekday()

    dagen_tot = (gewenste_dag - huidige_dag) % 7
    if dagen_tot == 0:
        eerste_tijd = dag_config.get("tijden", ["19:00"])[0]
        try:
            uur, minuut = map(int, eerste_tijd.split(":"))
            if nu.hour > uur or (nu.hour == uur and nu.minute >= minuut):
                dagen_tot = 7
        except ValueError:
            dagen_tot = 7

    target_date = vandaag_date + timedelta(days=dagen_tot)

    # Construeer een correcte TZ-aware datetime voor de acceptatie-check
    eerste_tijd = dag_config.get("tijden", ["19:00"])[0]
    try:
        uur, minuut = map(int, eerste_tijd.split(":"))
    except (ValueError, IndexError):
        uur, minuut = 19, 0
    target_met_tijd = datetime(
        target_date.year, target_date.month, target_date.day,
        uur, minuut, tzinfo=NL_TZ,
    )

    # Accepteer targets tot 35 min voorbij de 48u-grens.
    # Verkeerde-seizoen triggers komen ~60-80 min te vroeg (winter) of ~40 min
    # te laat (zomer) en worden dus nog steeds geweerd. De correcte trigger
    # start 20 min voor het venster, maar GHA kan tot 15 min vroeger starten.
    max_tijdstip = nu + timedelta(hours=uren_vooruit, minutes=35)
    if target_met_tijd > max_tijdstip:
        return None

    return target_date


def get_spelers(config: dict, dag: int) -> list[str]:
    """Haal de lijst met medespelers op voor een specifieke dag."""
    medespelers_config = config.get("medespelers", {})
    spelers_per_dag = medespelers_config.get("spelers_per_dag", {})
    if spelers_per_dag:
        # YAML keys kunnen int of string zijn afhankelijk van quoting;
        # controleer beide varianten
        if dag in spelers_per_dag:
            return spelers_per_dag[dag]
        if str(dag) in spelers_per_dag:
            return spelers_per_dag[str(dag)]
    return medespelers_config.get("standaard_spelers", [])


def splits_baan_voorkeur(baan_voorkeur: list[int], n_bots: int = 2) -> list[list[int]]:
    """
    Splits de baanvoorkeurlijst in n_bots delen.

    Verdeelt op even/oneven index zodat elke bot een top-voorkeur krijgt:
    [1, 3, 4, 2] -> Bot A: [1, 4], Bot B: [3, 2]

    Args:
        baan_voorkeur: Lijst met baannummers in volgorde van voorkeur.
        n_bots: Aantal bots (standaard 2).

    Returns:
        Lijst van lijsten, een per bot.
    """
    if not baan_voorkeur or n_bots <= 1:
        return [baan_voorkeur]

    delen = [[] for _ in range(n_bots)]
    for i, baan in enumerate(baan_voorkeur):
        delen[i % n_bots].append(baan)

    return delen


def bereken_venster_open(target_date: date, eerste_tijd: str, uren_vooruit: int) -> datetime:
    """
    Bereken het exacte moment waarop het reserveringsvenster opengaat.

    Accepteert zowel date als datetime. Bij een date-object wordt een
    correcte TZ-aware datetime geconstrueerd via de constructor (geen
    replace()) om DST-problemen te voorkomen.

    Returns:
        Timezone-aware datetime van het moment waarop het venster opent.
    """
    try:
        uur, minuut = map(int, eerste_tijd.split(":"))
    except ValueError:
        uur, minuut = 19, 0

    if isinstance(target_date, date) and not isinstance(target_date, datetime):
        reservering_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            uur, minuut, tzinfo=NL_TZ,
        )
    else:
        reservering_dt = target_date.replace(hour=uur, minute=minuut, second=0, microsecond=0)
        if reservering_dt.tzinfo is None:
            reservering_dt = reservering_dt.replace(tzinfo=NL_TZ)
    return reservering_dt - timedelta(hours=uren_vooruit)


def _wacht_tot(doel: datetime, label: str):
    """Wacht in een sleep-loop tot het doel-tijdstip is bereikt."""
    logger = logging.getLogger(__name__)
    nu = datetime.now(NL_TZ)
    wachttijd = (doel - nu).total_seconds()

    if wachttijd > 0:
        logger.info(f"{label} om: {doel.strftime('%H:%M:%S %Z')}")
        logger.info(f"Nog {wachttijd:.0f} seconden wachten ({wachttijd/60:.1f} minuten)...")

        while wachttijd > 0:
            slaap = min(wachttijd, 10)
            time.sleep(slaap)
            nu = datetime.now(NL_TZ)
            wachttijd = (doel - nu).total_seconds()
            if wachttijd > 0 and int(wachttijd) % 60 < 11:
                logger.info(f"Nog {wachttijd:.0f}s tot {label}...")
    else:
        logger.info(f"{label} al bereikt (sinds {abs(wachttijd):.0f}s geleden)")


def wacht_tot_voorbereiding(target_date: datetime, eerste_tijd: str, uren_vooruit: int):
    """Wacht tot VOORBEREIDING_MIN minuten voor het 48u-venster voor login + spelers."""
    venster_open = bereken_venster_open(target_date, eerste_tijd, uren_vooruit)
    start_voorbereiding = venster_open - timedelta(minutes=VOORBEREIDING_MIN)
    _wacht_tot(start_voorbereiding, "start voorbereiding")


def wacht_tot_48u_grens(target_date: datetime, eerste_tijd: str, uren_vooruit: int):
    """Wacht tot het 48u-reserveringsvenster open is."""
    venster_open = bereken_venster_open(target_date, eerste_tijd, uren_vooruit)
    _wacht_tot(venster_open, "reserveringsvenster")


def reserveer_met_retry(
    config: dict,
    dag_config: dict,
    dry_run: bool = False,
    verbose: bool = False,
    baan_voorkeur_override: list = None,
    bot_label: str = "",
    stop_event: threading.Event = None,
) -> dict:
    """
    Voer een reservering uit met retry-loop.

    Timing:
    1. Wacht tot T-3min (VOORBEREIDING_MIN voor het 48u-venster)
    2. Login + spelers selecteren
    3. Wacht tot T (48u-venster opent)
    4. Retry-loop tot T+3min (NA_VENSTER_MAX_MIN na het venster)
    5. Stop bij: succes, definitieve fout, stop_event, of deadline

    Args:
        config: Volledige configuratie.
        dag_config: Configuratie voor de specifieke dag.
        dry_run: Simulatiemodus.
        verbose: Extra debug output.
        baan_voorkeur_override: Optionele override van de baanvoorkeur
            (gebruikt bij parallelle pogingen om elke bot andere banen te laten proberen).
        bot_label: Optioneel label voor logging (bijv. "Bot-A").
        stop_event: Optioneel threading.Event dat aangeeft dat een andere bot al
            succesvol heeft gereserveerd. De retry-loop stopt als dit event is gezet.
    """
    logger = logging.getLogger(f"{__name__}.{bot_label}" if bot_label else __name__)
    dag = dag_config["dag"]
    tijden = dag_config.get("tijden", ["19:00"])
    uren_vooruit = config.get("reservering", {}).get("uren_vooruit", 48)
    baan_voorkeur = baan_voorkeur_override or config.get("reservering", {}).get("baan_voorkeur", [])

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
    label_str = f" [{bot_label}]" if bot_label else ""
    eerste_tijd = tijden[0]

    # Bereken absolute tijdstippen
    venster_open = bereken_venster_open(target_date, eerste_tijd, uren_vooruit)
    deadline = venster_open + timedelta(minutes=NA_VENSTER_MAX_MIN)

    logger.info("=" * 60)
    logger.info(f"RESERVERING MET RETRY{label_str} - {dagnaam} {target_date.strftime('%d-%m-%Y')}")
    logger.info(f"Tijden: {tijden} | Spelers: {spelers} | Banen: {baan_voorkeur}")
    logger.info(f"Venster opent: {venster_open.strftime('%H:%M:%S %Z')} | "
                f"Deadline: {deadline.strftime('%H:%M:%S %Z')}")
    logger.info("=" * 60)

    result = {
        "success": False,
        "datum": target_date.strftime("%d-%m-%Y"),
        "tijd": None,
        "baan": None,
        "spelers": spelers,
        "foutmelding": None,
    }

    bot = ApiReserveringBot(config, label=bot_label)
    try:
        # --- FASE 1: WACHT TOT T-3min ---
        logger.info(f"--- FASE 1{label_str}: Wachten tot voorbereiding ({VOORBEREIDING_MIN} min voor venster) ---")
        wacht_tot_voorbereiding(target_date, eerste_tijd, uren_vooruit)

        # --- FASE 2: LOGIN + SPELERS (3 min buffer voor venster) ---
        logger.info(f"--- FASE 2{label_str}: Voorbereiding (login + spelers) ---")
        bot.start()

        fout = bot.voorbereiden(target_date, tijden, spelers)
        if fout:
            result["foutmelding"] = fout
            logger.error(f"Voorbereiding mislukt{label_str}: {fout}")
            return result

        logger.info(f"Voorbereiding gelukt{label_str}! Klaar voor reservering.")

        # --- FASE 3: WACHT OP 48U-GRENS ---
        logger.info(f"--- FASE 3{label_str}: Wachten op reserveringsvenster ---")
        wacht_tot_48u_grens(target_date, eerste_tijd, uren_vooruit)

        # --- FASE 4: RETRY-LOOP (tot absolute deadline T+3min) ---
        logger.info(f"--- FASE 4{label_str}: Retry-loop tot {deadline.strftime('%H:%M:%S')} ---")
        poging_nr = 0

        while datetime.now(NL_TZ) < deadline:
            if stop_event and stop_event.is_set():
                logger.info(f"Stop-signaal ontvangen{label_str} - andere bot was succesvol")
                result["foutmelding"] = "Gestopt: andere bot heeft al gereserveerd"
                return result

            poging_nr += 1
            logger.info(f"--- Poging {poging_nr}{label_str} ({datetime.now(NL_TZ).strftime('%H:%M:%S')}) ---")

            poging = bot.probeer_reserveer(
                target_date, tijden, spelers, baan_voorkeur, dry_run,
                is_eerste_poging=(poging_nr == 1),
            )

            if poging["success"]:
                result["success"] = True
                result["tijd"] = poging["tijd"]
                result["baan"] = poging["baan"]
                result["foutmelding"] = poging.get("foutmelding")
                logger.info(f"SUCCES{label_str} na {poging_nr} poging(en): "
                            f"{poging['tijd']} op {poging['baan']}")
                if stop_event:
                    stop_event.set()
                return result

            if not poging["retry"]:
                result["foutmelding"] = poging["foutmelding"]
                logger.warning(f"Definitief mislukt{label_str} na {poging_nr} pogingen: "
                               f"{poging['foutmelding']}")
                return result

            # Check of we nog tijd hebben voor een volgende poging
            if datetime.now(NL_TZ) >= deadline:
                break

            logger.info(f"Nog niet gelukt{label_str} ({poging['foutmelding']}), "
                        f"volgende poging over {RETRY_INTERVAL_SEC}s...")
            time.sleep(RETRY_INTERVAL_SEC)

        # Deadline bereikt
        result["foutmelding"] = (
            f"Deadline bereikt{label_str} ({deadline.strftime('%H:%M:%S')}) "
            f"na {poging_nr} pogingen"
        )
        logger.error(result["foutmelding"])

    except Exception as e:
        result["foutmelding"] = f"Onverwachte fout{label_str}: {e}"
        logger.error(f"Onverwachte fout{label_str}: {e}", exc_info=True)
    finally:
        bot.stop()

    return result


def reserveer_parallel(config: dict, dag_config: dict, dry_run: bool = False, verbose: bool = False) -> dict:
    """
    Voer een reservering uit met twee parallelle bots, elk gericht op andere banen.

    Strategie:
    - Splits de baanvoorkeur in twee delen (bijv. [1,4] en [3,2])
    - Start twee onafhankelijke browser-sessies die tegelijkertijd de wizard doorlopen
    - De eerste bot die succesvol reserveert signaleert de andere om te stoppen
    - Als beide falen, worden de foutmeldingen gecombineerd

    Dit verhoogt de succeskans doordat twee banen tegelijk worden geprobeerd.
    De KNLTB-site blokkeert zelf dubbele boekingen, dus dit is veilig.
    """
    logger = logging.getLogger(__name__)
    dag = dag_config["dag"]
    baan_voorkeur = config.get("reservering", {}).get("baan_voorkeur", [])
    n_bots = config.get("reservering", {}).get("parallel_pogingen", 1)

    if n_bots <= 1 or len(baan_voorkeur) < 2:
        # Niet genoeg banen om te splitsen, gebruik standaard modus
        logger.info("Parallelle modus niet mogelijk (te weinig banen), gebruik standaard")
        return reserveer_met_retry(config, dag_config, dry_run=dry_run, verbose=verbose)

    # Splits baanvoorkeur over de bots
    voorkeur_delen = splits_baan_voorkeur(baan_voorkeur, n_bots)
    bot_labels = [f"Bot-{chr(65 + i)}" for i in range(n_bots)]  # Bot-A, Bot-B, ...

    logger.info("=" * 60)
    logger.info(f"PARALLELLE RESERVERING - {n_bots} bots tegelijk")
    for label, deel in zip(bot_labels, voorkeur_delen):
        logger.info(f"  {label}: banen {deel}")
    logger.info("=" * 60)

    # Gedeeld stop-event: zodra een bot slaagt, stoppen de anderen
    stop_event = threading.Event()

    def _run_bot(label: str, baan_deel: list) -> dict:
        """Wrapper voor een enkele bot in een thread."""
        try:
            return reserveer_met_retry(
                config=config,
                dag_config=dag_config,
                dry_run=dry_run,
                verbose=verbose,
                baan_voorkeur_override=baan_deel,
                bot_label=label,
                stop_event=stop_event,
            )
        except Exception as e:
            logger.error(f"Onverwachte fout in {label}: {e}", exc_info=True)
            return {
                "success": False,
                "datum": "n.v.t.",
                "tijd": None,
                "baan": None,
                "spelers": [],
                "foutmelding": f"Onverwachte fout in {label}: {e}",
            }

    # Start alle bots parallel
    resultaten = []
    with ThreadPoolExecutor(max_workers=n_bots) as executor:
        futures = {
            executor.submit(_run_bot, label, deel): label
            for label, deel in zip(bot_labels, voorkeur_delen)
        }

        for future in as_completed(futures):
            label = futures[future]
            try:
                result = future.result()
                resultaten.append((label, result))
                if result["success"]:
                    logger.info(f"{label} SUCCES: {result['tijd']} op {result['baan']}")
                else:
                    logger.info(f"{label} mislukt: {result.get('foutmelding', '?')}")
            except Exception as e:
                logger.error(f"{label} crashte: {e}")
                resultaten.append((label, {
                    "success": False, "datum": "n.v.t.", "tijd": None,
                    "baan": None, "spelers": [], "foutmelding": f"{label} crashte: {e}",
                }))

    # Bepaal het eindresultaat
    # Prioriteit: eerste succes; anders combineer foutmeldingen
    succes_resultaten = [(l, r) for l, r in resultaten if r["success"]]
    if succes_resultaten:
        winner_label, winner_result = succes_resultaten[0]
        logger.info(f"Parallelle reservering gelukt via {winner_label}: "
                     f"{winner_result['tijd']} op {winner_result['baan']}")
        return winner_result

    # Geen enkele bot slaagde - combineer foutmeldingen
    foutmeldingen = []
    spelers = []
    datum = "n.v.t."
    for label, result in resultaten:
        if result.get("foutmelding"):
            foutmeldingen.append(f"{label}: {result['foutmelding']}")
        if result.get("spelers"):
            spelers = result["spelers"]
        if result.get("datum") and result["datum"] != "n.v.t.":
            datum = result["datum"]

    gecombineerde_fout = " | ".join(foutmeldingen) if foutmeldingen else "Onbekende fout"
    logger.warning(f"Alle {n_bots} bots mislukt: {gecombineerde_fout}")

    return {
        "success": False,
        "datum": datum,
        "tijd": None,
        "baan": None,
        "spelers": spelers,
        "foutmelding": gecombineerde_fout,
    }


def reserveer_voor_dag(config: dict, dag_config: dict, dry_run: bool = False, verbose: bool = False) -> dict:
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

    bot = ApiReserveringBot(config)
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


def sync_spelers(config: dict):
    """Haal alle beschikbare spelers op van KNLTB en sla op als players.json."""
    logger = logging.getLogger(__name__)
    logger.info("=== Spelerslijst synchroniseren ===")

    players_file = BASE_DIR / "players.json"

    bot = ApiReserveringBot(config)
    try:
        bot.start()
        spelers = bot.haal_alle_spelers()
    finally:
        bot.stop()

    if not spelers:
        logger.error("Geen spelers gevonden op KNLTB - players.json niet bijgewerkt")
        return

    players_data = {
        "updated": datetime.now(NL_TZ).strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(spelers),
        "players": [
            {"name": naam, "guid": guid}
            for naam, guid in sorted(spelers.items())
        ],
    }

    with open(players_file, "w", encoding="utf-8") as f:
        json.dump(players_data, f, indent=2, ensure_ascii=False)

    logger.info(f"Spelerslijst opgeslagen: {len(spelers)} spelers -> {players_file}")
    for naam in sorted(spelers.keys()):
        logger.debug(f"  {naam}")


def dump_court_html(config: dict):
    """Dump de court-pagina HTML naar bestand voor diagnose."""
    logger = logging.getLogger(__name__)
    logger.info("=== HTML Dump Modus ===")

    dagen_config = config.get("reservering", {}).get("dagen", [])
    if not dagen_config:
        logger.error("Geen dagen geconfigureerd")
        return

    dag_config = dagen_config[0]
    dag = dag_config["dag"]
    tijden = dag_config.get("tijden", ["19:00"])
    uren_vooruit = config.get("reservering", {}).get("uren_vooruit", 48)
    spelers = get_spelers(config, dag)

    from api_bot import ApiReserveringBot
    bot = ApiReserveringBot(config, label="dump")
    try:
        bot.start()

        # Bereken target datum (forceer vandaag + dagen_tot)
        nu = datetime.now(NL_TZ)
        vandaag = nu.date()
        gewenste_dag = dag_config["dag"]
        dagen_tot = (gewenste_dag - vandaag.weekday()) % 7
        if dagen_tot == 0:
            dagen_tot = 7
        target_date = vandaag + timedelta(days=dagen_tot)

        logger.info(f"Target: {DAGNAMEN.get(dag, dag)} {target_date} {tijden}")

        fout = bot.voorbereiden(target_date, tijden, spelers)
        if fout:
            logger.error(f"Voorbereiding mislukt: {fout}")
            return

        court_html = bot._selecteer_dag(target_date, tijden)

        dump_file = BASE_DIR / "court_dump.html"
        with open(dump_file, "w", encoding="utf-8") as f:
            f.write(court_html)
        logger.info(f"Court HTML gedumpt naar: {dump_file} ({len(court_html)} bytes)")

        bot._parse_beschikbare_slots(court_html)

    except Exception as e:
        logger.error(f"Dump mislukt: {e}", exc_info=True)
    finally:
        bot.stop()


def main():
    """Hoofdfunctie."""
    parser = argparse.ArgumentParser(
        description="Automatische padelbaan reservering"
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
    parser.add_argument(
        "--sync-spelers",
        action="store_true",
        help="Haal spelerslijst op van KNLTB en sla op als players.json",
    )
    parser.add_argument(
        "--dump-html",
        action="store_true",
        help="Dump de court-pagina HTML naar bestand voor diagnose (geen reservering)",
    )
    args = parser.parse_args()

    # Setup
    setup_logging(verbose=args.verbose)
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Padel Reservering Bot")
    logger.info(f"Gestart op: {datetime.now(NL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")
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

    # Sync spelers modus
    if args.sync_spelers:
        sync_spelers(config)
        sys.exit(0)

    # Dump HTML modus (diagnose)
    if args.dump_html:
        dump_court_html(config)
        sys.exit(0)

    # Bepaal welke dag(en) we moeten reserveren
    if args.dag is not None:
        # Specifieke dag opgegeven via command line
        alle_dagen = config.get("reservering", {}).get("dagen", [])
        te_reserveren = [d for d in alle_dagen if d["dag"] == args.dag]
        if not te_reserveren:
            logger.error(f"Dag {args.dag} ({DAGNAMEN.get(args.dag, '?')}) "
                         f"niet geconfigureerd in config.yaml")
            sys.exit(1)
    else:
        # Automatisch: vind alle dagen die nu binnen het 48u-venster vallen
        te_reserveren = vind_reserveerbare_dagen(config)
        if not te_reserveren:
            logger.error("Geen reserveerbare dagen gevonden binnen het 48-uur venster. "
                         "Gebruik --dag <nummer> om een specifieke dag te forceren.")
            sys.exit(1)

    # E-mail notifier
    email_config = config.get("email", {})
    notifier = EmailNotifier(email_config)

    # Bepaal of we parallelle modus gebruiken
    parallel_pogingen = config.get("reservering", {}).get("parallel_pogingen", 1)
    if parallel_pogingen > 1:
        logger.info(f"Parallelle modus: {parallel_pogingen} bots tegelijk")

    # Voer reserveringen uit voor alle gevonden dagen
    resultaten = []
    for dag_config in te_reserveren:
        dagnaam = DAGNAMEN.get(dag_config["dag"], str(dag_config["dag"]))
        logger.info(f"\n--- Reservering voor: {dagnaam} ---")

        if args.no_retry:
            result = reserveer_voor_dag(config, dag_config, dry_run=args.dry_run, verbose=args.verbose)
        elif parallel_pogingen > 1:
            result = reserveer_parallel(config, dag_config, dry_run=args.dry_run, verbose=args.verbose)
        else:
            result = reserveer_met_retry(config, dag_config, dry_run=args.dry_run, verbose=args.verbose)

        resultaten.append(result)

        # Verstuur notificatie per reservering
        if not args.dry_run:
            notifier.verstuur(result)
        else:
            logger.info("DRY RUN - Geen e-mail verstuurd")

        if result["success"]:
            logger.info(f"SUCCES: {dagnaam} {result['datum']} om {result['tijd']} "
                         f"op baan {result['baan']}")
        else:
            logger.warning(f"MISLUKT: {dagnaam} - {result.get('foutmelding', 'onbekende fout')}")

    # Samenvatting
    logger.info("\n" + "=" * 60)
    logger.info("SAMENVATTING")
    logger.info("=" * 60)
    geslaagd = sum(1 for r in resultaten if r["success"])
    mislukt = len(resultaten) - geslaagd
    logger.info(f"Totaal: {len(resultaten)} | Geslaagd: {geslaagd} | Mislukt: {mislukt}")

    for result in resultaten:
        status = "OK" if result["success"] else "FOUT"
        logger.info(f"  [{status}] {result['datum']} {result.get('tijd', '-')} "
                     f"- {result.get('foutmelding') or 'Gelukt'}")

    logger.info("=" * 60)

    sys.exit(0 if mislukt == 0 else 1)


if __name__ == "__main__":
    main()

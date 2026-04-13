"""
setup_cron.py - Genereer en installeer crontab entries op basis van config.yaml.

Leest de reserveringsdagen en -tijden uit config.yaml, berekent wanneer de bot
moet starten (VOORBEREIDING_MIN voor het 48u-venster) en installeert de
bijbehorende crontab entries. Bestaande niet-padel entries blijven behouden.

Gebruik:
    python3 setup_cron.py          # Genereer en installeer crontab
    python3 setup_cron.py -q       # Stil: alleen installeren als er iets veranderd is
    python3 setup_cron.py --dry-run # Toon wat er zou worden geinstalleerd
"""

import argparse
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent
CRON_MARKER_START = "# --- padel-reservering START ---"
CRON_MARKER_END = "# --- padel-reservering END ---"
VOORBEREIDING_MIN = 3

DAGNAMEN = {
    0: "maandag", 1: "dinsdag", 2: "woensdag", 3: "donderdag",
    4: "vrijdag", 5: "zaterdag", 6: "zondag",
}

# Python weekday (0=ma) -> cron weekday (0=zo, 1=ma, ..., 6=za)
PYTHON_TO_CRON_DOW = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}


def load_config() -> dict:
    config_path = BASE_DIR / "config.yaml"
    if not config_path.exists():
        print(f"FOUT: {config_path} niet gevonden", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def bereken_cron_entries(config: dict) -> list[str]:
    """Bereken crontab entries voor alle geconfigureerde reserveringsdagen."""
    uren_vooruit = config.get("reservering", {}).get("uren_vooruit", 48)
    dagen = config.get("reservering", {}).get("dagen", [])
    run_script = BASE_DIR / "run.sh"

    entries = []
    for dag_config in dagen:
        dag = dag_config["dag"]
        eerste_tijd = dag_config.get("tijden", ["19:00"])[0]
        dagnaam = DAGNAMEN.get(dag, str(dag))

        try:
            uur, minuut = map(int, eerste_tijd.split(":"))
        except ValueError:
            uur, minuut = 19, 0

        # Bereken wanneer het venster opent: speeltijd - uren_vooruit
        # We rekenen in minuten-van-de-dag en dagen-offset
        venster_min = uur * 60 + minuut - VOORBEREIDING_MIN
        venster_dagen_terug = uren_vooruit // 24
        venster_uren_rest = uren_vooruit % 24
        venster_min -= venster_uren_rest * 60

        # Normaliseer als minuten negatief worden (wrap naar vorige dag)
        while venster_min < 0:
            venster_min += 24 * 60
            venster_dagen_terug += 1

        cron_uur = venster_min // 60
        cron_minuut = venster_min % 60

        # Bereken de dag van de week voor cron
        # Python weekday: dag is de speeldag, we gaan venster_dagen_terug terug
        cron_python_dag = (dag - venster_dagen_terug) % 7
        cron_dow = PYTHON_TO_CRON_DOW[cron_python_dag]
        cron_dagnaam = DAGNAMEN.get(cron_python_dag, str(cron_python_dag))

        entries.append(
            f"# {dagnaam.capitalize()} {eerste_tijd} -> "
            f"venster opent {cron_dagnaam} {cron_uur:02d}:{cron_minuut + VOORBEREIDING_MIN:02d} -> "
            f"start {cron_dagnaam} {cron_uur:02d}:{cron_minuut:02d}"
        )
        entries.append(
            f"{cron_minuut} {cron_uur} * * {cron_dow} {run_script}"
        )

    # Auto-sync: elk uur config ophalen van GitHub
    sync_script = BASE_DIR / "sync.sh"
    entries.append("")
    entries.append("# Elk uur config syncen van GitHub en crontab bijwerken")
    entries.append(f"0 * * * * {sync_script}")

    return entries


def get_current_crontab() -> str:
    """Haal de huidige crontab op."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False,
        )
        return result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def build_new_crontab(current: str, padel_entries: list[str]) -> str:
    """Vervang padel-entries in de bestaande crontab, behoud de rest."""
    lines = current.splitlines()
    new_lines = []
    skipping = False

    for line in lines:
        if line.strip() == CRON_MARKER_START:
            skipping = True
            continue
        if line.strip() == CRON_MARKER_END:
            skipping = False
            continue
        if not skipping:
            new_lines.append(line)

    # Verwijder trailing lege regels
    while new_lines and not new_lines[-1].strip():
        new_lines.pop()

    # Voeg padel entries toe
    if new_lines:
        new_lines.append("")
    new_lines.append(CRON_MARKER_START)
    new_lines.extend(padel_entries)
    new_lines.append(CRON_MARKER_END)
    new_lines.append("")

    return "\n".join(new_lines)


def install_crontab(content: str):
    """Installeer de nieuwe crontab."""
    subprocess.run(
        ["crontab", "-"], input=content, text=True, check=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Genereer crontab uit config.yaml")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Stil: alleen installeren als er iets veranderd is")
    parser.add_argument("--dry-run", action="store_true",
                        help="Toon de crontab zonder te installeren")
    args = parser.parse_args()

    config = load_config()
    padel_entries = bereken_cron_entries(config)
    current = get_current_crontab()
    new_crontab = build_new_crontab(current, padel_entries)

    if args.dry_run:
        print("=== Gegenereerde crontab ===")
        print(new_crontab)
        return

    # In quiet mode: skip als er niets veranderd is
    if args.quiet and new_crontab.strip() == current.strip():
        return

    install_crontab(new_crontab)
    if not args.quiet:
        print("Crontab bijgewerkt:")
        for entry in padel_entries:
            print(f"  {entry}")


if __name__ == "__main__":
    main()

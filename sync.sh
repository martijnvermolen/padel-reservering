#!/bin/bash
# Sync config.yaml van GitHub en werk crontab bij als er iets veranderd is.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Haal laatste versie op
git pull -q 2>/dev/null

# Werk crontab bij (quiet mode: alleen als config veranderd is)
python3 setup_cron.py -q 2>/dev/null

#!/bin/bash
# Wrapper script voor padel reservering via cron.
# Laadt credentials uit .env en start de bot.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

python3 main.py --verbose > /dev/null 2>&1

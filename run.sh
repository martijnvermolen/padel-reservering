#!/bin/bash
# Wrapper script voor padel reservering via cron.
# Laadt credentials uit .env en start de bot.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

LOGFILE="$SCRIPT_DIR/cron.log"

# Roteer log als groter dan 1MB
if [ -f "$LOGFILE" ] && [ "$(stat -f%z "$LOGFILE" 2>/dev/null || stat -c%s "$LOGFILE" 2>/dev/null)" -gt 1048576 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOGFILE"
python3 main.py --verbose >> "$LOGFILE" 2>&1

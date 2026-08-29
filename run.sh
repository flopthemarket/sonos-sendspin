#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE="/data/options.json"

if [ -f "$OPTIONS_FILE" ]; then
    export SSG_OPTIONS_FILE="$OPTIONS_FILE"
else
    echo "[run.sh] No /data/options.json found - falling back to env-var / defaults (standalone/dev mode)"
fi

exec python3 -u /app/app/main.py

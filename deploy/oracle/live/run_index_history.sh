#!/bin/sh
# Official NSE index history for all indices (owner 2026-11-20). cron 17:05 + 20:05 IST; one run at a time.
cd "$HOME/gplm-live" || exit 1
mkdir -p index
LOG="index/index-$(date +%Y%m%d).log"
exec flock -n index/.lock "$HOME/stock-gplm-venv/bin/python" -u index_history.py >> "$LOG" 2>&1 < /dev/null

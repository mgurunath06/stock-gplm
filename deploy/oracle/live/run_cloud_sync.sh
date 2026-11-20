#!/bin/sh
# Watchdog start for the cloud filler (cron @reboot and every 5 min): starts it only if not running.
cd "$HOME/gplm-live" || exit 1
pgrep -f "python cloud_sync.py" >/dev/null && exit 0
LOG="sync-$(date +%Y%m%d).log"
echo "$(date '+%F %T') watchdog: starting cloud_sync" >> "$LOG"
setsid nohup "$HOME/stock-gplm-venv/bin/python" cloud_sync.py --minute-sessions 250 --order symbol \
  --universe universe.txt --floor 2026-01-19 >> "$LOG" 2>&1 < /dev/null &

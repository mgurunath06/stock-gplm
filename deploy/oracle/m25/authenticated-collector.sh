#!/usr/bin/env bash
set -euo pipefail
umask 077

AUTH="/home/ubuntu/stock-gplm-auth-venv/bin/stock-gplm-auth"
CONFIG="/etc/stock-gplm/m25-auth.json"
COLLECTOR="/home/ubuntu/StockGPLMDownloads/scripts/run_scheduled.sh"

: "${CREDENTIALS_DIRECTORY:?run this wrapper only from an approved systemd credential context}"
"$AUTH" ensure --config "$CONFIG" --allow-oauth-login
exec "$COLLECTOR"

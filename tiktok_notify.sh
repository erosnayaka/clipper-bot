#!/bin/bash
# Kirim pesan Telegram via bot. Usage: tiktok_notify.sh <chat_id> <message>
# Token dibaca dari file .bot_token di sebelah script ini (jangan di-commit!).
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
CHAT_ID="$1"; shift
MSG="$*"
TOKEN="$(cat "$BASE/.bot_token")"
PROXY="${HTTPS_PROXY:-${https_proxy:-}}"
curl -s ${PROXY:+-x "$PROXY"} --max-time 30 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" --data-urlencode "text=${MSG}" -o /dev/null -w "%{http_code}"
echo

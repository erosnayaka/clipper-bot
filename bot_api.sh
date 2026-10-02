#!/bin/bash
# bot_api.sh <method> <jsonfile> — POST JSON payload ke Bot API Telegram.
# Token dibaca dari file .bot_token di sebelah script ini (jangan di-commit!).
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
TOKEN="$(cat "$BASE/.bot_token")"
curl -s -m 30 -X POST "https://api.telegram.org/bot${TOKEN}/$1" \
  -H "Content-Type: application/json" -d @"$2"
echo

#!/bin/bash
# Jalankan ClipperBot (restart-safe).
# Butuh: ALLOWED_TELEGRAM_IDS di environment, token di .bot_token
BOTDIR="$(cd "$(dirname "$0")" && pwd)"
pkill -f "[c]lipper-bot/bot.py" 2>/dev/null
# tunggu instance lama benar-benar mati (graceful stop PTB butuh beberapa detik);
# kalau langsung start, getUpdates rebutan -> Conflict
for i in $(seq 1 15); do
  pgrep -f "[c]lipper-bot/bot.py" >/dev/null 2>&1 || break
  sleep 1
done
cd "$BOTDIR"
nohup python3 "$BOTDIR/bot.py" >> "$BOTDIR/bot.log" 2>&1 &
echo "ClipperBot PID $!"

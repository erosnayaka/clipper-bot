# ClipperBot ✂️

Bot Telegram buat motong video YouTube / Google Drive / MEGA jadi klip-klip
siap upload, lengkap dengan subtitle Indonesia yang dibakar ke video,
arsip klip, dan antrean upload TikTok terjadwal.

## Fitur

- **Link YouTube** → pilih *Cari momen viral* (analisis transkrip + komentar,
  ngasih 3 kandidat momen ~3 menit, tinggal tap) atau *Potong manual*
  (kirim jam mulai → jam selesai). Bisa juga sebaris:
  `<link> 1:00 2:30 1080p nosubs`
- **Link GDrive / MEGA** → otomatis download, buang intro/outro, dipecah jadi
  klip ±5 menit. **Anti-crash**: proses jalan detached, kalau bot restart /
  crash dia lanjut otomatis dari bagian terakhir (nggak ngulang download,
  nggak dobel kirim).
- **Multi-link** → tempel banyak link dalam satu pesan, semuanya diproses.
- **Subtitle Indonesia** otomatis dibakar ke video (bisa ON/OFF).
- **`/arsip`** → semua klip kesimpen di sini; bisa hapus per klip, ada estimasi
  jam posting buat yang antre.
- **`/upload`** → pilih klip → atur caption (otomatis / ketik manual / template
  hashtag via `/tag`) → masuk antrean TikTok.
- **Antrean TikTok** → 1 klip per jeda (`/jeda`, default 3 jam). Bisa batalin
  per klip. Caption massal buat hasil pecahan cloud, termasuk penomoran
  otomatis (`Judul | 1`, `| 2`, ...).
- Bot privat: cuma Telegram user ID di allowlist yang bisa pakai.

## Instalasi

```bash
git clone <repo> clipper-bot && cd clipper-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg harus kepasang di sistem (apt install ffmpeg)
```

## Konfigurasi

1. Buat bot via [@BotFather](https://t.me/BotFather), simpan token ke file
   `.bot_token` di folder ini (**jangan di-commit!**).
2. Cari Telegram user ID lo via [@userinfobot](https://t.me/userinfobot).
3. Set environment (atau copy `.env.example` jadi `.env`):

```bash
export ALLOWED_TELEGRAM_IDS="123456789"
```

Opsional: `CLIPPER_VENV_PY`, `CLIPPER_YT_CLIP`, `CLIPPER_GDRIVE_SPLIT`,
`TT_INTERVAL_STATE` (lihat `.env.example`).

## Jalanin

```bash
bash start.sh
```

## Upload TikTok

Antrean upload TikTok (`tiktok_queue/`) diproses worker terpisah — repo ini
cuma nulis job JSON ke sana. Contoh worker: cron tiap 3 menit yang ambil 1 job
terbaru, upload via browser automation ke akun TikTok sendiri (akun login
manual sekali di awal), lalu notifikasi via `tiktok_notify.sh`.

## Struktur

```
bot.py            — bot Telegram (semua fitur di atas)
yt-clip.py        — potong 1 segmen YouTube + burn subtitle
gdrive-split.py   — download GDrive/MEGA + pecah jadi klip (resumable)
start.sh          — start/restart bot
bot_api.sh        — helper POST ke Bot API (dipakai worker cron)
tiktok_notify.sh  — kirim notifikasi Telegram dari shell
```

## Catatan jujur

- Kualitas YouTube dari IP datacenter biasanya mentok ~360p (itag 18);
  output 720p/1080p adalah upscale + denoise, bukan HD asli.
- YouTube kadang throttle IP shared → bot retry otomatis 3x lalu lapor error.
- MEGA: link harus menyertakan key dekripsi; file >2GB ditolak; kuota
  download gratis dibagi dengan IP yang sama.

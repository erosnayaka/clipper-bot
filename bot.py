#!/usr/bin/env python3
"""ClipperBot — Bot Telegram auto-clipper video YouTube.

Alur: kirim link -> kirim jam mulai -> kirim jam selesai -> pilih resolusi
      -> video jadi dikirim balik. Atau sekali kirim:
      <link youtube> <mulai> <selesai> [480p|720p|1080p|asli] [nosubs]
"""
import asyncio
import datetime
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, ConversationHandler,
                          MessageHandler, filters)
from telegram.request import HTTPXRequest

BASE = os.path.dirname(os.path.abspath(__file__))
# Path tool — bisa dioverride lewat environment variable.
VENV_PY = os.environ.get("CLIPPER_VENV_PY", sys.executable)
CLIP_SCRIPT = os.environ.get("CLIPPER_YT_CLIP",
                             os.path.join(BASE, "yt-clip.py"))
RESOLUTIONS = ["480p", "720p", "1080p", "asli"]

# allowlist: cuma Telegram user ID ini yang boleh pakai.
# Set lewat env: ALLOWED_TELEGRAM_IDS="123456,789012"
# Kosong = tidak ada yang boleh pakai (aman secara default).
ALLOWLIST = {int(x) for x in
             os.environ.get("ALLOWED_TELEGRAM_IDS", "").replace(" ", "").split(",")
             if x.isdigit()}

# antrean upload TikTok: bot nulis job, cron worker yang upload via browser
TT_QUEUE = os.path.join(BASE, "tiktok_queue")
TT_PENDING = os.path.join(BASE, "tiktok_pending")
TT_FAILED = os.path.join(BASE, "tiktok_failed")
for _d in (TT_QUEUE, TT_PENDING, TT_FAILED):
    os.makedirs(_d, exist_ok=True)

# antrean analisis momen viral: bot nulis job, cron worker yang analisis
VIRAL_Q = os.path.join(BASE, "viral_queue")
VIRAL_C = os.path.join(BASE, "viral_candidates")
for _d in (VIRAL_Q, VIRAL_C):
    os.makedirs(_d, exist_ok=True)

# Conversation states
ASK_START, ASK_END, ASK_RES = range(3)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s",
                    level=logging.INFO)
# jangan log URL API (berisi token)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("clipperbot")


def read_token() -> str:
    with open(os.path.join(BASE, ".bot_token")) as f:
        return f.read().strip()


def fixed_proxy(raw: str) -> str:
    """Encode userinfo proxy agar httpx tidak gagal parse."""
    parts = urlsplit(raw)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        if ":" in userinfo:
            user, pw = userinfo.split(":", 1)
            userinfo = f"{quote(user, safe='')}:{quote(pw, safe='')}"
        else:
            userinfo = quote(userinfo, safe="")
        netloc = f"{userinfo}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query,
                       parts.fragment))


def parse_time(s: str) -> float | None:
    s = s.strip().replace(",", ".")
    try:
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return float(s)
        parts = s.split(":")
        if len(parts) > 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
            return None
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    except ValueError:
        return None


def extract_urls(text: str) -> list:
    """Semua link YouTube di teks (unik, urut kemunculan)."""
    pat = r"https?://(?:www\.)?(?:youtube\.com/watch\?\S+|youtu\.be/\S+)"
    out = []
    for m in re.finditer(pat, text):
        u = m.group(0).rstrip(").>,")
        if u not in out:
            out.append(u)
    return out


def extract_url(text: str) -> str | None:
    urls = extract_urls(text)
    return urls[0] if urls else None


def extract_cloud_urls(text: str) -> list:
    """Semua link GDrive/MEGA di teks (unik, urut kemunculan)."""
    pat = r"https?://(drive\.google\.com|" + r"mega" + r"\.nz)/\S+"
    out = []
    for m in re.finditer(pat, text):
        u = m.group(0).rstrip(").>,")
        if u not in out:
            out.append(u)
    return out


def extract_cloud_url(text: str) -> str | None:
    urls = extract_cloud_urls(text)
    return urls[0] if urls else None


def _short_link(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if m:
        return f"youtu.be/{m.group(1)}"
    return url[:42] + "…" if len(url) > 42 else url


GDRIVE_SCRIPT = os.environ.get("CLIPPER_GDRIVE_SPLIT",
                             os.path.join(BASE, "gdrive-split.py"))

# Job cloud (GDrive/MEGA) yang anti-crash: script split jalan detached
# (tetap hidup walau bot restart), state di cloud_jobs/<jobid>.json,
# progres dipantau lewat log. Recovery jalan tiap bot start.
CLOUD_JOBS = os.path.join(BASE, "cloud_jobs")
os.makedirs(CLOUD_JOBS, exist_ok=True)
_cloud_procs = {}  # jobid -> subprocess.Popen (hanya yg dibuat proses ini)
CLOUD_MAX_RESTART = 3
CLOUD_EXPIRE_S = 48 * 3600
CLOUD_STALL_S = 90 * 60
CLOUD_DELIVER_BACKOFF_S = 600


def _cloud_path(jobid):
    return os.path.join(CLOUD_JOBS, f"{jobid}.json")


def _cloud_save(job):
    p = _cloud_path(job["job_id"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(job, f)
    os.replace(tmp, p)


def _cloud_load(jobid):
    try:
        with open(_cloud_path(jobid)) as f:
            return json.load(f)
    except Exception:
        return None


def _cloud_list():
    try:
        return [f[:-5] for f in os.listdir(CLOUD_JOBS) if f.endswith(".json")]
    except OSError:
        return []


def _cloud_cleanup(job, keep_log=False):
    jobid = job["job_id"]
    shutil.rmtree(job.get("outdir") or "", ignore_errors=True)
    for ext in (".json", ".log"):
        if ext == ".log" and keep_log:
            continue
        try:
            os.remove(os.path.join(CLOUD_JOBS, f"{jobid}{ext}"))
        except OSError:
            pass
    _cloud_procs.pop(jobid, None)


def _pid_is_script(pid, jobid):
    """Pastikan pid masih milik script job ini (cegah pid-reuse)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        return "gdrive-split" in cmd and jobid in cmd
    except Exception:
        return False


def _chunk_idx(path):
    m = re.search(r"_p(\d+)\.mp4$", path or "")
    return int(m.group(1)) if m else 0


async def _cloud_edit(job, bot, text):
    try:
        await bot.edit_message_text(text, chat_id=job["chat_id"],
                                    message_id=job.get("msg_id"))
    except Exception:
        pass


def _cloud_launch(job):
    """Jalankan gdrive-split.py detached (survive bot restart)."""
    jobid = job["job_id"]
    logpath = os.path.join(CLOUD_JOBS, f"{jobid}.log")
    lf = open(logpath, "a")
    try:
        proc = subprocess.Popen(
            [VENV_PY, GDRIVE_SCRIPT, job["url"], job["outdir"], jobid,
             "--intro", "135", "--outro", "85"],
            stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        lf.close()
    job["pid"] = proc.pid
    job["last_log_ts"] = time.time()
    _cloud_procs[jobid] = proc
    _cloud_save(job)
    log.info("cloud launch job %s pid %d", jobid, proc.pid)


async def _cloud_fail(job, bot, reason):
    try:
        await bot.send_message(job["chat_id"],
                               f"❌ {job['src']} gagal: {reason}")
    except Exception:
        pass
    try:
        await bot.delete_message(job["chat_id"], job.get("msg_id"))
    except Exception:
        pass
    _cloud_cleanup(job, keep_log=True)
    log.warning("cloud job %s gagal: %s", job["job_id"], reason)


async def _cloud_deliver(job, bot):
    """Kirim klip ke Telegram (idempotent). Return 'done' / 'stalled'."""
    jobid = job["job_id"]
    srcl = job["src"].lower()
    await _cloud_edit(job, bot,
                      f"📤 {job['src']} jadi {len(job['chunks'])} klip, ngirim ke Telegram...")
    await asyncio.sleep(0)  # biar adil ke loop

    def fmt(s):
        s = int(s)
        return f"{s // 60}:{s % 60:02d}"

    idxs = sorted(int(k) for k in job["chunks"])
    delivered = set(job.get("delivered", []))
    for i in idxs:
        if i in delivered:
            continue
        ch = job["chunks"][str(i)]
        cjob = f"{jobid}p{i}"
        pending = os.path.join(TT_PENDING, f"{cjob}.mp4")
        if not os.path.exists(pending):
            if ch.get("path") and os.path.exists(ch["path"]):
                shutil.move(ch["path"], pending)
            else:
                log.warning("cloud chunk hilang %s p%d", jobid, i)
                delivered.add(i)
                continue
        metap = os.path.join(TT_PENDING, f"{cjob}.json")
        if not os.path.exists(metap):
            with open(metap, "w") as f:
                json.dump({"start_s": fmt(ch["s"]), "end_s": fmt(ch["e"]),
                           "res": "720p", "title": job.get("title") or "gdrive",
                           "reason": f"{srcl} part {i}"}, f)
        ok = False
        for attempt in range(3):
            try:
                kb = [[InlineKeyboardButton(
                    "📤 Upload ke TikTok",
                    callback_data=f"ttu:{cjob}")]]
                with open(pending, "rb") as vf:
                    await bot.send_video(
                        job["chat_id"], video=vf,
                        caption=f"🎬 Part {i} ({fmt(ch['s'])} - {fmt(ch['e'])})",
                        supports_streaming=True,
                        reply_markup=InlineKeyboardMarkup(kb))
                ok = True
                break
            except Exception as e:
                log.warning("cloud send gagal %s p%d (%d/3): %s",
                            jobid, i, attempt + 1, e)
                await asyncio.sleep(20)
        if not ok:
            job["delivered"] = sorted(delivered)
            job["deliver_backoff_until"] = time.time() + CLOUD_DELIVER_BACKOFF_S
            _cloud_save(job)
            if not job.get("stall_notified"):
                job["stall_notified"] = True
                _cloud_save(job)
                try:
                    await bot.send_message(
                        job["chat_id"],
                        "⏸️ Telegram lagi susah dihubungi pas ngirim klip, "
                        "gue coba lagi otomatis...")
                except Exception:
                    pass
            return "stalled"
        delivered.add(i)
        job["delivered"] = sorted(delivered)
        job["stall_notified"] = False
        _cloud_save(job)
    if not job.get("summary_sent"):
        kb = [[InlineKeyboardButton("✏️ Caption manual semua part",
                                   callback_data=f"gcap:{jobid}")]]
        try:
            await bot.send_message(
                job["chat_id"],
                f"✅ Jadi {len(idxs)} klip, masuk arsip semua.\n"
                "Pake /upload buat milih mana yang naik ke TikTok,\n"
                "atau isi caption manual buat semua part langsung 👇",
                reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        job["summary_sent"] = True
        _cloud_save(job)
    try:
        await bot.delete_message(job["chat_id"], job.get("msg_id"))
    except Exception:
        pass
    _cloud_cleanup(job)
    log.info("cloud job %s selesai", jobid)
    return "done"


async def _cloud_watch_once(jobid, bot):
    """Satu putaran pantau. Return True kalau job selesai."""
    job = _cloud_load(jobid)
    if not job:
        return True
    # 1. baca baris log baru
    logpath = os.path.join(CLOUD_JOBS, f"{jobid}.log")
    try:
        with open(logpath, "rb") as f:
            f.seek(job.get("log_offset", 0))
            data = f.read()
            job["log_offset"] = f.tell()
    except FileNotFoundError:
        data = b""
    if data:
        job["last_log_ts"] = time.time()
    changed = bool(data)
    for raw in data.splitlines():
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("STEP:"):
            txt = line[5:].strip()
            if time.time() - job.get("last_edit", 0) > 8:
                job["last_edit"] = time.time()
                await _cloud_edit(job, bot, f"📥 {job['src']} mode\n{txt}")
        elif line.startswith("TITLE:"):
            job["title"] = line[6:].strip()[:80]
        elif line.startswith("CHUNK:"):
            p = line[6:].strip().split()
            idx = _chunk_idx(p[0]) if len(p) >= 3 else 0
            if idx:
                job["chunks"][str(idx)] = {
                    "path": p[0], "s": float(p[1]), "e": float(p[2])}
        elif line.startswith("ERROR:"):
            job["error"] = line[6:].strip()[:300]
        elif line.startswith("DONE:"):
            job["script_done"] = True
    if changed:
        _cloud_save(job)

    # 2. cek proses script masih hidup?
    proc = _cloud_procs.get(jobid)
    if proc is not None:
        alive = proc.poll() is None
    elif job.get("pid"):
        alive = _pid_is_script(job["pid"], jobid)
    else:
        alive = False
    if alive:
        # stall: hidup tapi 90 menit tanpa output apa pun -> kill & resume
        if time.time() - job.get("last_log_ts", job["created"]) > CLOUD_STALL_S \
                and job.get("restarts", 0) < CLOUD_MAX_RESTART:
            log.warning("cloud job %s stall, kill & resume", jobid)
            try:
                if proc is not None:
                    proc.kill()
                else:
                    os.kill(job["pid"], 9)
            except Exception:
                pass
            alive = False
        else:
            return False

    # proses mati -> reap
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        _cloud_procs.pop(jobid, None)

    if job.get("script_done") and job.get("stage") == "splitting":
        job["stage"] = "delivering"
        _cloud_save(job)
    if job.get("stage") == "delivering":
        if time.time() < job.get("deliver_backoff_until", 0):
            return False
        res = await _cloud_deliver(job, bot)
        return res == "done"
    if job.get("error"):
        await _cloud_fail(job, bot, job["error"])
        return True
    # mati tanpa DONE/ERROR -> kepotong (kill/restart/crash): resume
    if job.get("restarts", 0) >= CLOUD_MAX_RESTART:
        await _cloud_fail(job, bot, "proses mati 3x, nyerah")
        return True
    job["restarts"] = job.get("restarts", 0) + 1
    _cloud_save(job)
    await _cloud_edit(job, bot,
                      f"🔄 Proses {job['src']} kepotong, lanjut otomatis "
                      f"({job['restarts']}/{CLOUD_MAX_RESTART})...")
    _cloud_launch(job)
    return False


async def _cloud_watch(jobid, bot):
    while True:
        try:
            if await _cloud_watch_once(jobid, bot):
                return
        except Exception:
            log.exception("cloud watch error %s", jobid)
        await asyncio.sleep(10)


async def _cloud_recover(app):
    """Jalan tiap bot start: sambung lagi job cloud yang belum selesai."""
    bot = app.bot
    now = time.time()
    for jobid in _cloud_list():
        job = _cloud_load(jobid)
        if not job:
            continue
        if now - job.get("created", 0) > CLOUD_EXPIRE_S:
            log.info("cloud job %s kadaluarsa, cleanup", jobid)
            _cloud_cleanup(job)
            continue
        try:
            msg = await bot.send_message(
                job["chat_id"],
                f"🔄 Bot restart, job {job['src']} gue pantau lagi...")
            job["msg_id"] = msg.message_id
        except Exception:
            pass
        _cloud_save(job)
        asyncio.create_task(_cloud_watch(jobid, bot))
        log.info("cloud recover job %s stage=%s", jobid, job.get("stage"))


async def _post_init(app):
    await _cloud_recover(app)


async def on_gdrive(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Mode GDrive/MEGA anti-crash: script split jalan detached,
    progres dipantau watcher, bisa resume kapan pun."""
    chat = update.effective_chat
    src = "MEGA" if re.search(r"mega" + r"\.nz", url) else "GDrive"
    for jid in _cloud_list():
        j = _cloud_load(jid)
        if j and j.get("url") == url:
            await update.message.reply_text(
                f"⏳ Link {j['src']} ini lagi diproses, sabar coy...")
            return
    jobid = uuid.uuid4().hex[:12]
    outdir = os.path.join(BASE, "gdrive_out", jobid)
    os.makedirs(outdir, exist_ok=True)
    job = {"job_id": jobid, "url": url, "src": src,
           "chat_id": chat.id, "user_id": update.effective_user.id,
           "outdir": outdir, "stage": "splitting",
           "title": "", "chunks": {}, "delivered": [],
           "created": time.time(), "restarts": 0,
           "log_offset": 0, "last_log_ts": time.time()}
    _cloud_save(job)
    _cloud_launch(job)
    msg = await context.bot.send_message(
        chat.id, f"📥 Link {src} diterima!\n⬇️ Download dulu...")
    job["msg_id"] = msg.message_id
    _cloud_save(job)
    asyncio.create_task(_cloud_watch(jobid, context.bot))
    log.info("cloud job %s mulai (%s)", jobid, src)


def _enqueue_tiktok(jobid, caption, chat_id, user_id):
    """Masukkan klip arsip ke antrean upload TikTok."""
    job = {
        "job_id": jobid,
        "video": os.path.join(TT_PENDING, f"{jobid}.mp4"),
        "caption": caption,
        "chat_id": chat_id,
        "user_id": user_id,
        "privacy": "public",
        "attempts": 0,
        "created": time.time(),
    }
    with open(os.path.join(TT_QUEUE, f"{jobid}.json"), "w") as f:
        json.dump(job, f)


def _read_pending_meta(jobid):
    try:
        with open(os.path.join(TT_PENDING, f"{jobid}.json")) as f:
            return json.load(f)
    except Exception:
        return {}


async def on_gcap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol caption manual semua part (mode gdrive): tanya caption per part."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    jobid = query.data.split(":", 1)[1]
    parts = []
    try:
        for f in sorted(os.listdir(TT_PENDING)):
            if f.startswith(f"{jobid}p") and f.endswith(".json"):
                cj = f[:-5]
                if os.path.exists(os.path.join(TT_PENDING, f"{cj}.mp4")) \
                        and not os.path.exists(os.path.join(TT_QUEUE, f"{cj}.json")):
                    parts.append(cj)
    except OSError:
        pass

    def pnum(cj):
        try:
            return int(cj.rsplit("p", 1)[1])
        except (ValueError, IndexError):
            return 0

    parts.sort(key=pnum)
    if not parts:
        await query.message.reply_text(
            "Semua part udah masuk antrean / filenya nggak ada.")
        return
    context.user_data["gcap"] = {"parts": parts, "idx": 0, "mode": None,
                                "done": 0}
    kb = [[InlineKeyboardButton("\U0001f522 Judul + nomor otomatis",
                               callback_data="gcapmode:auto")],
          [InlineKeyboardButton("\u270f\ufe0f Isi caption per part",
                               callback_data="gcapmode:manual")]]
    await query.message.reply_text(
        f"\u270f\ufe0f Caption manual: {len(parts)} part.\n"
        "Mau gue nomorin otomatis, atau isi satu-satu?",
        reply_markup=InlineKeyboardMarkup(kb))


async def on_gcap_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pilihan mode caption gdrive: nomor otomatis vs isi per part."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    st = context.user_data.get("gcap")
    if not st:
        await query.message.reply_text(
            "Sesi caption-nya udah habis, tap tombolnya lagi ya.")
        return
    mode = query.data.split(":", 1)[1]
    n = len(st["parts"])
    if mode == "auto":
        st["mode"] = "auto"
        st["step"] = "title"
        await query.message.reply_text(
            "Kirim judulnya (boleh + hashtag, pisahin baris baru).\n"
            "Contoh:\n"
            "<pre>HISOKA VS CASTRO\n#hunterxhunter #anime #fyp</pre>\n\n"
            "Nanti gue jadiin <code>HISOKA VS CASTRO | 1</code>, "
            "<code>| 2</code>, dst.\n"
            "Ketik 'batal' kalau nggak jadi.",
            parse_mode="HTML")
    else:
        st["mode"] = "manual"
        await query.message.reply_text(
            f"Kirim caption buat Part 1/{n}:\n"
            "(kirim 'skip' buat pake caption otomatis, 'batal' buat berhenti)")


async def _on_gcap_auto_text(update, context, st, text):
    """Langkah judul/nomor pada mode nomor otomatis."""
    parts = st["parts"]
    if st.get("step", "title") == "title":
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        head = lines[0][:150]
        tail = "\n".join(lines[1:])
        st["head"], st["tail"] = head, tail
        st["step"] = "number"
        await update.message.reply_text(
            f"Judul: <code>{html.escape(head)}</code>\n"
            "Mulai dari nomor berapa? (kirim angka, atau 'skip' = 1)",
            parse_mode="HTML")
        return
    if text.lower() == "skip":
        start = 1
    elif text.isdigit():
        start = max(int(text), 1)
    else:
        await update.message.reply_text(
            "Kirim angka aja ya (atau 'skip' = mulai dari 1).")
        return
    head, tail = st["head"], st.get("tail", "")
    preset = _get_hashtags()
    auto_tags = bool(preset and "#" not in (head + tail))
    for i, cjob in enumerate(parts):
        caption = f"{head} | {start + i}"
        if tail:
            caption += f"\n{tail}"
        if auto_tags:
            caption += f"\n{preset}"
        _enqueue_tiktok(cjob, caption[:2200], update.effective_chat.id,
                        update.effective_user.id)
    n = len(parts)
    context.user_data.pop("gcap", None)
    tag_note = " (hashtag favorit ditambah otomatis)" if auto_tags else ""
    await update.message.reply_text(
        f"\u2705 {n} part masuk antrean dengan nomor otomatis "
        f"(<code>{html.escape(head)} | {start}</code> s/d "
        f"<code>| {start + n - 1}</code>){tag_note}.",
        parse_mode="HTML")


async def on_gcap_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima caption (mode gdrive): judul+nomor otomatis atau per part."""
    st = context.user_data.get("gcap")
    if not st:
        return
    parts = st["parts"]
    text = update.message.text.strip()
    if text.lower() == "batal":
        done = st.get("done", 0)
        context.user_data.pop("gcap", None)
        extra = f" {done} part yang udah diisi tetap antre." if done else ""
        await update.message.reply_text(f"Dibatalkan.{extra}")
        return
    if st.get("mode") == "auto":
        await _on_gcap_auto_text(update, context, st, text)
        return
    if st.get("mode") is None:
        # user langsung ketik tanpa pilih mode -> anggap isi per part,
        # teks ini langsung jadi caption Part 1
        st["mode"] = "manual"
    idx = st["idx"]
    cjob = parts[idx]
    if text.lower() == "skip":
        caption = build_auto_caption(_read_pending_meta(cjob))
    else:
        caption = text[:2200]
    _enqueue_tiktok(cjob, caption, update.effective_chat.id,
                    update.effective_user.id)
    st["done"] = st.get("done", 0) + 1
    cur = idx + 1
    idx += 1
    if idx >= len(parts):
        context.user_data.pop("gcap", None)
        await update.message.reply_text(
            f"✅ {len(parts)} klip masuk antrean upload TikTok. "
            "Gue kabarin per klip kalau udah keposting.")
    else:
        st["idx"] = idx
        await update.message.reply_text(
            f"✅ Part {cur} oke.\n"
            f"Kirim caption buat Part {cur + 1}/{len(parts)}:")


def is_allowed(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ALLOWLIST


async def guard(update: Update) -> bool:
    if not is_allowed(update):
        await update.message.reply_text("Maaf, bot ini privat.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Halo! Kirim link YouTube, nanti gue tanya jam mulai, jam selesai, "
        "dan resolusi.\n\nAtau sekali kirim:\n"
        "<code>link mulai selesai [resolusi]</code>\n"
        "Contoh: <code>https://youtu.be/xxx 1:00 2:30 720p</code>\n\n"
        "Resolusi: 480p / 720p / 1080p / asli (default 720p). Maks 10 menit.\n\n"
        "Abis klip jadi, ada tombol 📤 Upload ke TikTok buat autoposting.",
        parse_mode="HTML")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Dibatalkan.")
    return ConversationHandler.END


HASHTAG_FILE = os.path.join(BASE, "hashtag_preset.json")


def _get_hashtags():
    """Return preset hashtag favorit, atau '' kalau belum diset."""
    try:
        with open(HASHTAG_FILE) as f:
            return (json.load(f).get("tags") or "").strip()
    except Exception:
        return ""


def _set_hashtags(tags):
    with open(HASHTAG_FILE, "w") as f:
        json.dump({"tags": tags}, f)


def _norm_hashtags(raw):
    """Normalisasi: tiap tag diawali #, dipisah spasi, maks 500 char."""
    tags = []
    for w in raw.split():
        w = w.strip()
        if not w:
            continue
        if not w.startswith("#"):
            w = "#" + w
        if w not in tags:
            tags.append(w)
    return " ".join(tags)[:500]


async def on_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tag: lihat/set/hapus hashtag favorit buat caption manual."""
    if not is_allowed(update):
        return
    args = context.args or []
    if not args:
        cur = _get_hashtags()
        if cur:
            await update.message.reply_text(
                f"Hashtag favorit:\n<code>{html.escape(cur)}</code>\n\n"
                "Ganti: <code>/tag #anime #fyp</code>\n"
                "Hapus: <code>/tag hapus</code>",
                parse_mode="HTML")
        else:
            await update.message.reply_text(
                "Belum ada hashtag favorit.\n"
                "Set dengan: <code>/tag #hunterxhunter #anime #fyp</code>",
                parse_mode="HTML")
        return
    if args[0].lower() == "hapus":
        _set_hashtags("")
        await update.message.reply_text("Hashtag favorit dihapus.")
        return
    tags = _norm_hashtags(" ".join(args))
    if not tags:
        await update.message.reply_text(
            "Formatnya: /tag #anime #fyp (atau /tag hapus).")
        return
    _set_hashtags(tags)
    await update.message.reply_text(
        f"\u2705 Hashtag favorit disimpan:\n<code>{html.escape(tags)}</code>",
        parse_mode="HTML")


def _enqueue_tiktok_job(jobid, caption, chat_id, user_id):
    """Tulis job upload TikTok ke antrean. Return True kalau berhasil."""
    if os.path.exists(os.path.join(TT_QUEUE, f"{jobid}.json")):
        return False
    if not os.path.exists(os.path.join(TT_PENDING, f"{jobid}.mp4")):
        return False
    job = {
        "job_id": jobid,
        "video": os.path.join(TT_PENDING, f"{jobid}.mp4"),
        "caption": caption,
        "chat_id": chat_id,
        "user_id": user_id,
        "privacy": "public",
        "attempts": 0,
        "created": time.time(),
    }
    with open(os.path.join(TT_QUEUE, f"{jobid}.json"), "w") as f:
        json.dump(job, f)
    return True


async def on_tt_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima caption TikTok lalu masukkan job ke antrean."""
    jobid = context.user_data.pop("awaiting_tt_caption", None)
    if not jobid:
        return
    meta = {}
    try:
        with open(os.path.join(TT_PENDING, f"{jobid}.json")) as f:
            meta = json.load(f)
    except Exception:
        pass
    text = update.message.text.strip()
    if text.lower() == "batal":
        context.user_data.pop("tt_auto_caption", None)
        await update.message.reply_text("Dibatalkan, nggak jadi masuk antrean.")
        return
    if text.lower() == "ok":
        caption = context.user_data.pop("tt_auto_caption", None)
        if not caption:
            caption = build_auto_caption(meta)
    elif text.lower() == "skip":
        context.user_data.pop("tt_auto_caption", None)
        caption = build_auto_caption(meta)
    else:
        context.user_data.pop("tt_auto_caption", None)
        caption = text[:2200]
        preset = _get_hashtags()
        if preset and "#" not in caption:
            # tawarkan hashtag favorit sekali tap (tanpa ubah diam-diam)
            context.user_data["tt_pending_tags"] = {"jobid": jobid,
                                                   "caption": caption}
            kb = [[InlineKeyboardButton("\u2795 Tambah hashtag favorit",
                                       callback_data=f"tttags:yes:{jobid}")],
                  [InlineKeyboardButton("\u2705 Gini aja",
                                       callback_data=f"tttags:no:{jobid}")]]
            await update.message.reply_text(
                "Caption:\n\n"
                f"<pre>{html.escape(caption)}</pre>\n\n"
                "Mau sekalian tambah hashtag favorit?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb))
            return
    if _enqueue_tiktok_job(jobid, caption, update.effective_chat.id,
                           update.effective_user.id):
        await update.message.reply_text(
            "✅ Masuk antrean upload TikTok. Gue kabarin di sini kalau udah keposting.")
    else:
        await update.message.reply_text(
            "Udah masuk antrean / file klipnya udah nggak ada.")


async def on_tttags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Jawaban tawaran hashtag favorit pada caption manual /upload."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    _, choice, jobid = query.data.split(":", 2)
    st = context.user_data.pop("tt_pending_tags", None)
    if not st or st.get("jobid") != jobid:
        await query.message.reply_text("Sesi caption-nya udah habis.")
        return
    caption = st["caption"]
    if choice == "yes":
        preset = _get_hashtags()
        if preset:
            caption = f"{caption}\n{preset}"[:2200]
    if _enqueue_tiktok_job(jobid, caption, update.effective_chat.id,
                           update.effective_user.id):
        await query.message.reply_text(
            "✅ Masuk antrean upload TikTok. Gue kabarin di sini kalau udah keposting.")
    else:
        await query.message.reply_text(
            "Udah masuk antrean / file klipnya udah nggak ada.")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: terima link, atau one-liner lengkap."""
    if not await guard(update):
        return ConversationHandler.END
    # lagi nunggu caption TikTok? tangani di sini (di luar conversation)
    if context.user_data.get("gcap"):
        await on_gcap_text(update, context)
        return ConversationHandler.END
    if context.user_data.get("awaiting_tt_caption"):
        await on_tt_caption(update, context)
        return ConversationHandler.END
    text = update.message.text.strip()
    gurls = extract_cloud_urls(text)
    yurls = extract_urls(text)
    total = len(gurls) + len(yurls)
    if total == 0:
        await update.message.reply_text(
            "Kirim link YouTube / GDrive / MEGA.\n"
            "Boleh banyak sekaligus dalam satu pesan.")
        return ConversationHandler.END
    if total > 1:
        return await on_multilink(update, context, gurls, yurls)
    if gurls:
        return await on_gdrive(update, context, gurls[0])
    url = yurls[0]
    if not url:
        await update.message.reply_text(
            "Kirim link YouTube yang valid (youtube.com / youtu.be).")
        return ConversationHandler.END

    # one-liner: <url> <start> <end> [res] [nosubs]
    rest = text.replace(url, "").strip().split()
    toks = [t for t in rest if t.lower() != "nosubs"]
    burn_subs = len(toks) == len(rest)
    if len(toks) >= 2:
        s, e = parse_time(toks[0]), parse_time(toks[1])
        res = toks[2].lower() if len(toks) >= 3 else "720p"
        if s is None or e is None or e <= s:
            await update.message.reply_text(
                "Format waktu salah. Contoh: <code>link 1:00 2:30 720p</code>",
                parse_mode="HTML")
            return ConversationHandler.END
        if res not in RESOLUTIONS:
            res = "720p"
        context.user_data.update(url=url, start_s=toks[0], end_s=toks[1],
                                 res=res, burn_subs=burn_subs)
        return await do_clip(update, context)

    lid = uuid.uuid4().hex[:8]
    context.user_data.setdefault("pending_links", {})[lid] = url
    kb = [
        [InlineKeyboardButton("🔥 Cari momen viral", callback_data=f"viral:{lid}")],
        [InlineKeyboardButton("✂️ Potong manual", callback_data=f"manual:{lid}")],
    ]
    await update.message.reply_text(
        "Link diterima! Mau yang mana?",
        reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def on_multilink(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    gurls: list, yurls: list):
    """Pesan berisi >1 link: cloud langsung diproses paralel,
    tiap link YouTube dapat tombol viral/manual sendiri."""
    for u in gurls:
        await on_gdrive(update, context, u)
    if yurls:
        links = context.user_data.setdefault("pending_links", {})
        kb = []
        lines = []
        for n, u in enumerate(yurls, 1):
            lid = uuid.uuid4().hex[:8]
            links[lid] = u
            lines.append(f"{n}. {_short_link(u)}")
            kb.append([
                InlineKeyboardButton(f"🔥 Viral #{n}",
                                     callback_data=f"viral:{lid}"),
                InlineKeyboardButton(f"✂️ Manual #{n}",
                                     callback_data=f"manual:{lid}"),
            ])
        await update.message.reply_text(
            f"🔗 Ketemu {len(yurls)} link YouTube:\n" + "\n".join(lines) +
            "\n\nTap 🔥 buat cari momen viral, ✂️ buat potong manual "
            "(satu-satu ya).",
            reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def on_link_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pilihan setelah kirim link: cari momen viral atau potong manual."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return ConversationHandler.END
    kind, lid = query.data.split(":", 1)
    url = context.user_data.get("pending_links", {}).get(lid)
    if not url:
        await query.edit_message_text("Link-nya udah kedaluwarsa, kirim ulang ya.")
        return ConversationHandler.END
    if kind == "manual":
        context.user_data["url"] = url
        await query.edit_message_text(
            "Mau dipotong dari menit ke berapa? (contoh: 1:30)")
        return ASK_START
    # viral: masukkan ke antrean analisis
    jobid = uuid.uuid4().hex[:12]
    job = {"jobid": jobid, "url": url,
           "chat_id": update.effective_chat.id, "created": time.time()}
    with open(os.path.join(VIRAL_Q, f"{jobid}.json"), "w") as f:
        json.dump(job, f)
    await query.edit_message_text(
        "🔥 Lagi gue analisis videonya (transkrip + komen penonton)...\n"
        "Tunggu ~2 menit, nanti gue kasih 3 momen paling rame.")
    log.info("viral job %s queued for %s", jobid, update.effective_user.id)
    return ConversationHandler.END


async def on_vclip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tap kandidat momen viral -> langsung jadiin klip."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    try:
        _, jobid, idx = query.data.split(":")
        with open(os.path.join(VIRAL_C, f"{jobid}.json")) as f:
            data = json.load(f)
        c = data["candidates"][int(idx)]
    except Exception:
        await query.edit_message_text(
            "Kandidatnya udah kedaluwarsa, analisis ulang ya.")
        return
    context.user_data.update(url=data["url"], start_s=c["start"],
                             end_s=c["end"], res="1080p",
                             reason=c.get("reason", ""),
                             burn_subs=data.get("burn_subs", True))
    await do_clip(update, context, via_query=True)


async def on_vsub_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle subtitle ON/OFF untuk kandidat momen viral (per job analisis)."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    jobid = query.data.split(":", 1)[1]
    path = os.path.join(VIRAL_C, f"{jobid}.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        await query.edit_message_text(
            "Kandidatnya udah kedaluwarsa, analisis ulang ya.")
        return
    data["burn_subs"] = not data.get("burn_subs", True)
    with open(path, "w") as f:
        json.dump(data, f)
    nums = ["1️⃣", "2️⃣", "3️⃣"]
    kb = []
    for i, c in enumerate(data["candidates"]):
        kb.append([InlineKeyboardButton(
            f"{nums[i]} {c['start']}–{c['end']}",
            callback_data=f"vclip:{jobid}:{i}")])
    kb.append([InlineKeyboardButton(
        "\U0001F524 Subtitle: ON" if data["burn_subs"]
        else "\U0001F524 Subtitle: OFF",
        callback_data=f"vsub:{jobid}")])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))


async def on_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = parse_time(update.message.text)
    if s is None:
        await update.message.reply_text("Format salah. Contoh: 1:00 atau 90")
        return ASK_START
    context.user_data["start_s"] = update.message.text.strip()
    await update.message.reply_text("Sampai menit ke berapa? (contoh: 2:30)")
    return ASK_END


async def on_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    e = parse_time(update.message.text)
    s = parse_time(context.user_data["start_s"])
    if e is None or e <= s:
        await update.message.reply_text(
            "Waktu selesai harus lebih besar dari waktu mulai. Coba lagi:")
        return ASK_END
    if e - s > 600:
        await update.message.reply_text("Maksimal 10 menit. Coba lagi:")
        return ASK_END
    context.user_data["end_s"] = update.message.text.strip()
    context.user_data["burn_subs"] = True
    kb = [
        [InlineKeyboardButton(r, callback_data=f"res:{r}") for r in RESOLUTIONS],
        [InlineKeyboardButton("\U0001F524 Subtitle: ON",
                              callback_data="subs:toggle")],
    ]
    await update.message.reply_text("Pilih resolusi:",
                                    reply_markup=InlineKeyboardMarkup(kb))
    return ASK_RES


async def on_subs_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle subtitle ON/OFF di alur potong manual (tetap di ASK_RES)."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    cur = context.user_data.get("burn_subs", True)
    context.user_data["burn_subs"] = not cur
    kb = [
        [InlineKeyboardButton(r, callback_data=f"res:{r}") for r in RESOLUTIONS],
        [InlineKeyboardButton(
            "\U0001F524 Subtitle: ON" if not cur else "\U0001F524 Subtitle: OFF",
            callback_data="subs:toggle")],
    ]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))


async def on_res(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    res = query.data.split(":", 1)[1]
    context.user_data["res"] = res
    await query.edit_message_text(f"Resolusi: {res}. Lagi motong, tunggu ya...")
    # kirim ke chat asli (bukan edit) biar gampang: pakai message dari query
    await do_clip(update, context, via_query=True)
    return ConversationHandler.END


async def run_clip(url: str, start_s: str, end_s: str, res: str,
                 burn_subs: bool = True) -> tuple[str | None, str, str]:
    out = f"/tmp/clip_{os.getpid()}.mp4"
    cmd = [VENV_PY, CLIP_SCRIPT, url, start_s, end_s,
           "--res", res, "--out", out]
    if burn_subs:
        cmd.append("--burn-subs")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        return None, "Kelamaan, coba klip yang lebih pendek.", ""
    text = raw.decode(errors="replace")
    m = re.search(r"^OUTPUT:(.+)$", text, re.M)
    t = re.search(r"^TITLE:(.+)$", text, re.M)
    title = t.group(1).strip() if t else ""
    if m and os.path.exists(m.group(1).strip()):
        return m.group(1).strip(), "", title
    err = re.search(r"^ERROR:(.+)$", text, re.M)
    if not m:
        log.warning("clip gagal %s-%s: %s", start_s, end_s, text[-1500:])
    return None, (err.group(1).strip() if err else "Gagal motong, coba lagi."), title


async def yt_probe(url: str) -> bool:
    """Cek cepat (~40 dtk) apakah YouTube bisa dijangkau.
    Biar nggak bakar 10 menit buat klip yang pasti gagal saat YouTube down."""
    cmd = [VENV_PY, "-m", "yt_dlp", "--no-playlist", "--socket-timeout", "20",
           "--extractor-args", "youtube:player_client=android",
           "--get-title", url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=40)
        return proc.returncode == 0
    except Exception:
        return False


async def do_clip(update: Update, context: ContextTypes.DEFAULT_TYPE, via_query=False):
    d = context.user_data
    chat = update.effective_chat
    msg = await context.bot.send_message(chat.id, "Lagi motong videonya...")
    await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_VIDEO)

    # 1. probe cepat: kalau YouTube lagi nggak bisa dijangkau, bilang langsung
    if not await yt_probe(d["url"]):
        try:
            await msg.delete()
        except Exception:
            pass
        target = update.callback_query.message if via_query else update.message
        await target.reply_text(
            "\u26a0\ufe0f YouTube lagi susah dijangkau dari server (koneksi timeout). "
            "Bukan salah klipnya \u2014 coba tap lagi 5\u201310 menit lagi ya.")
        context.user_data.clear()
        return

    # 2. klip dengan retry otomatis 3x (brownout YouTube sering cuma bbrp menit)
    path, err, title = None, "", ""
    for attempt in (1, 2, 3):
        if attempt > 1:
            try:
                await msg.edit_text(
                    f"\U0001F504 YouTube-nya lagi lemot, gue coba lagi otomatis "
                    f"({attempt}/3)...")
            except Exception:
                pass
            await asyncio.sleep(90)
            await context.bot.send_chat_action(chat.id, ChatAction.UPLOAD_VIDEO)
        path, err, title = await run_clip(d["url"], d["start_s"], d["end_s"],
                                          d.get("res", "720p"),
                                          d.get("burn_subs", True))
        if path and os.path.exists(path) and os.path.getsize(path) >= 10_000:
            break
        if path:  # file ada tapi rusak/<10KB -> anggap gagal, coba lagi
            path, err = None, "hasil klip rusak (file kosong)."
        log.warning("clip attempt %d gagal %s-%s: %s", attempt,
                    d["start_s"], d["end_s"], err)
    try:
        await msg.delete()
    except Exception:
        pass
    if not path:
        target = update.callback_query.message if via_query else update.message
        await target.reply_text(
            f"\u274c Gagal 3x: {err}\nYouTube-nya lagi bermasalah dari server, "
            f"coba tap lagi 10\u201315 menit lagi ya.")
    else:
        size_mb = os.path.getsize(path) / 1e6
        caption = f"Klip {d['start_s']} - {d['end_s']} ({d.get('res', '720p')})"
        jobid = uuid.uuid4().hex[:12]
        pending = os.path.join(TT_PENDING, f"{jobid}.mp4")
        shutil.move(path, pending)
        with open(os.path.join(TT_PENDING, f"{jobid}.json"), "w") as f:
            json.dump({"start_s": d["start_s"], "end_s": d["end_s"],
                       "res": d.get("res", "720p"),
                       "title": title, "reason": d.get("reason", "")}, f)
        kb = [[InlineKeyboardButton("\U0001F4E4 Upload ke TikTok",
                                   callback_data=f"ttu:{jobid}")]]
        await context.bot.send_video(chat.id, video=open(pending, "rb"),
                                     caption=caption,
                                     supports_streaming=True,
                                     reply_markup=InlineKeyboardMarkup(kb))
        log.info("clip ok %.1fMB for %s job %s", size_mb,
                 update.effective_user.id, jobid)
    context.user_data.clear()


def build_auto_caption(meta: dict) -> str:
    """Bikin caption TikTok otomatis dari judul video + alasan momen viral."""
    title = (meta.get("title") or "").strip()
    reason = (meta.get("reason") or "").strip()
    tags = ["#fyp", "#viral", "#clip"]
    stop = {"yang", "dari", "dengan", "untuk", "sama", "ini", "itu", "dan",
            "atau", "dengan", "the", "and", "with"}
    for w in re.findall(r"[A-Za-z0-9]+", (title + " " + reason).lower()):
        if len(w) >= 4 and w not in stop and f"#{w}" not in tags \
                and len(tags) < 6:
            tags.append(f"#{w}")
    if reason:
        head = reason[0].upper() + reason[1:] + " \U0001F525"
    elif title:
        head = f"\U0001F3AC {title}"
    else:
        s, e = meta.get("start_s", ""), meta.get("end_s", "")
        head = f"\U0001F3AC Klip {s} - {e}".strip()
    parts = [head]
    if reason and title:
        parts.append(title)
    parts.append(" ".join(tags))
    return "\n\n".join(parts)[:500]


async def on_tiktok_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol Upload ke TikTok -> pilih caption otomatis atau tulis manual."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    jobid = query.data.split(":", 1)[1]
    if os.path.exists(os.path.join(TT_QUEUE, f"{jobid}.json")):
        await query.message.reply_text("Udah masuk antrean, tunggu ya.")
        return
    if not os.path.exists(os.path.join(TT_PENDING, f"{jobid}.mp4")):
        await query.message.reply_text("File klipnya udah nggak ada, klip ulang ya.")
        return
    meta = {}
    try:
        with open(os.path.join(TT_PENDING, f"{jobid}.json")) as f:
            meta = json.load(f)
    except Exception:
        pass
    auto = build_auto_caption(meta)
    context.user_data["tt_auto_caption"] = auto
    kb = [[InlineKeyboardButton("\u2705 Pakai caption otomatis",
                               callback_data=f"ttcap:ok:{jobid}")],
          [InlineKeyboardButton("\u270f\ufe0f Tulis caption manual",
                               callback_data=f"ttcap:manual:{jobid}")]]
    await query.message.reply_text(
        "Caption buat klip ini:\n\n"
        f"<pre>{html.escape(auto)}</pre>\n\n"
        "Mau pakai yang otomatis, atau tulis sendiri?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb))


async def on_ttcap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pilihan caption di /upload: otomatis langsung antre, manual -> ketik."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    _, mode, jobid = query.data.split(":", 2)
    if mode == "manual":
        if os.path.exists(os.path.join(TT_QUEUE, f"{jobid}.json")):
            await query.message.reply_text("Udah masuk antrean, tunggu ya.")
            return
        if not os.path.exists(os.path.join(TT_PENDING, f"{jobid}.mp4")):
            await query.message.reply_text(
                "File klipnya udah nggak ada, klip ulang ya.")
            return
        context.user_data["awaiting_tt_caption"] = jobid
        await query.message.reply_text(
            "Oke, kirim caption manualnya sekarang (maks 2200 karakter).\n"
            "Ketik 'batal' kalau nggak jadi.")
        return
    # mode == "ok": langsung antre dengan caption otomatis
    caption = context.user_data.pop("tt_auto_caption", None)
    if not caption:
        try:
            with open(os.path.join(TT_PENDING, f"{jobid}.json")) as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        caption = build_auto_caption(meta)
    context.user_data.pop("awaiting_tt_caption", None)
    if _enqueue_tiktok_job(jobid, caption, update.effective_chat.id,
                           update.effective_user.id):
        await query.message.reply_text(
            "✅ Masuk antrean upload TikTok. Gue kabarin di sini kalau udah keposting.")
    else:
        await query.message.reply_text(
            "Udah masuk antrean / file klipnya udah nggak ada.")


def _arsip_rows():
    """Return (rows, items, total_mb); items = [(jid, title, is_queued)]."""
    try:
        queued = {f[:-5] for f in os.listdir(TT_QUEUE) if f.endswith(".json")}
    except OSError:
        queued = set()
    etas = _queue_eta_map() if queued else {}
    rows, items, total_mb = [], [], 0.0
    try:
        # urutan arsip = waktu klip dibuat (jobid acak, abjad tidak valid)
        metas = sorted(
            (f for f in os.listdir(TT_PENDING) if f.endswith(".json")),
            key=lambda f: os.path.getmtime(os.path.join(TT_PENDING, f)))
    except OSError:
        metas = []
    for mf in metas:
        jid = mf[:-5]
        mp4 = os.path.join(TT_PENDING, f"{jid}.mp4")
        if not os.path.exists(mp4):
            continue
        try:
            with open(os.path.join(TT_PENDING, mf)) as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        title = (meta.get("title") or "klip").strip()[:40]
        se = f"{meta.get('start_s', '?')}-{meta.get('end_s', '?')}"
        mb = os.path.getsize(mp4) / 1e6
        total_mb += mb
        is_q = jid in queued
        if is_q:
            eta = _fmt_eta(etas.get(jid)) if jid in etas else "?"
            st = f"\u23f3 antre TikTok (~{eta})"
        else:
            st = "\U0001f4e6 belum diupload"
        rows.append(f"\u2022 {title} ({se}, {mb:.0f}MB) \u2014 {st}")
        items.append((jid, title, is_q))
    return rows, items, total_mb


def _arsip_text_kb():
    rows, items, total_mb = _arsip_rows()
    n_queued = sum(1 for _, _, q in items if q)
    if not rows:
        txt = ("\U0001f4ed Nggak ada klip yang kesimpen di server.\n"
               "Klip yang udah keposting TikTok otomatis dihapus biar hemat tempat.")
        return txt, None
    txt = (f"\U0001f3ac Klip kesimpen: {len(rows)} "
           f"({total_mb:.0f}MB), {n_queued} antre TikTok.\n\n" + "\n".join(rows))
    kb = []
    for jid, title, is_q in items:
        if not is_q:
            kb.append([InlineKeyboardButton(f"\U0001f5d1\ufe0f {title[:24]}",
                                           callback_data=f"del:{jid}")])
        else:
            kb.append([InlineKeyboardButton(f"\u274c Batalkan upload: {title[:24]}",
                                           callback_data=f"qcancel:{jid}")])
    if any(not q for _, _, q in items):
        kb.append([InlineKeyboardButton("\U0001f9f9 Hapus semua (belum diupload)",
                                       callback_data="del:all")])
    return txt, (InlineKeyboardMarkup(kb) if kb else None)


async def on_arsip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/arsip: daftar klip yang masih kesimpen di server + status upload TikTok."""
    if not is_allowed(update):
        return
    txt, kb = _arsip_text_kb()
    await update.message.reply_text(txt, reply_markup=kb)


async def on_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol hapus di /arsip: konfirmasi dulu, baru hapus mp4+json."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return

    async def rerender():
        txt, kb = _arsip_text_kb()
        try:
            await query.edit_message_text(txt, reply_markup=kb)
        except Exception:
            pass

    parts = query.data.split(":")
    target = parts[1]

    if target == "cancel":
        await rerender()
        return

    # minta konfirmasi dulu
    if len(parts) == 2:
        what = "semua klip yang belum diupload" if target == "all" else "klip ini"
        kb = [[InlineKeyboardButton(f"\u26a0\ufe0f Yakin, hapus {what}",
                                   callback_data=f"del:{target}:yes")],
              [InlineKeyboardButton("Batal", callback_data="del:cancel")]]
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return

    # eksekusi hapus
    if target == "all":
        victims = [jid for jid, _, q in _arsip_rows()[1] if not q]
    else:
        if os.path.exists(os.path.join(TT_QUEUE, f"{target}.json")):
            await query.answer("Lagi antre TikTok, nggak bisa dihapus.",
                               show_alert=True)
            await rerender()
            return
        victims = [target]
    n, freed = 0, 0.0
    for jid in victims:
        mp4 = os.path.join(TT_PENDING, f"{jid}.mp4")
        if os.path.exists(mp4):
            freed += os.path.getsize(mp4) / 1e6
        for ext in (".mp4", ".json"):
            try:
                os.remove(os.path.join(TT_PENDING, f"{jid}{ext}"))
            except OSError:
                pass
        n += 1
    log.info("arsip hapus %d klip (%.0fMB) oleh %s", n, freed,
             update.effective_user.id)
    await rerender()
    try:
        await query.answer(f"\U0001f5d1\ufe0f {n} klip dihapus ({freed:.0f}MB).")
    except Exception:
        pass


async def on_qcancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol batalkan antrean TikTok di /arsip: konfirmasi dulu, baru hapus job queue.

    Videonya tetap di arsip (TT_PENDING) jadi bisa diantrekan lagi via /upload.
    Kalau job lagi dikunci worker (mid-upload), tolak dengan pesan jujur.
    """
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return

    async def rerender():
        txt, kb = _arsip_text_kb()
        try:
            await query.edit_message_text(txt, reply_markup=kb)
        except Exception:
            pass

    parts = query.data.split(":")
    target = parts[1]

    if target == "cancel":
        await rerender()
        return

    # minta konfirmasi dulu
    if len(parts) == 2:
        kb = [[InlineKeyboardButton("\u26a0\ufe0f Yakin, batalkan upload",
                                   callback_data=f"qcancel:{target}:yes")],
              [InlineKeyboardButton("Batal", callback_data="qcancel:cancel")]]
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return

    # eksekusi batalkan
    qjson = os.path.join(TT_QUEUE, f"{target}.json")
    if not os.path.exists(qjson):
        try:
            await query.answer("Udah nggak di antrean (mungkin udah keposting).",
                               show_alert=True)
        except Exception:
            pass
        await rerender()
        return
    if os.path.exists(os.path.join(TT_QUEUE, f"{target}.lock")):
        try:
            await query.answer("Lagi proses upload, nggak bisa dibatalin.",
                               show_alert=True)
        except Exception:
            pass
        await rerender()
        return
    try:
        os.remove(qjson)
    except OSError:
        pass
    log.info("arsip batalkan antrean TikTok %s oleh %s", target,
             update.effective_user.id)
    await rerender()
    try:
        await query.answer("\u2705 Upload dibatalkan, klip tetap di arsip.")
    except Exception:
        pass


def _pending_unqueued():
    """List (jobid, meta) klip di tiktok_pending yang belum masuk antrean."""
    try:
        queued = {f[:-5] for f in os.listdir(TT_QUEUE) if f.endswith(".json")}
    except OSError:
        queued = set()
    try:
        # urutan = waktu klip dibuat (jobid acak, abjad tidak valid)
        files = sorted(os.listdir(TT_PENDING),
                       key=lambda f: os.path.getmtime(os.path.join(TT_PENDING, f)))
    except OSError:
        return []
    out = []
    for mf in files:
        if not mf.endswith(".json"):
            continue
        jid = mf[:-5]
        if jid in queued:
            continue
        if not os.path.exists(os.path.join(TT_PENDING, f"{jid}.mp4")):
            continue
        try:
            with open(os.path.join(TT_PENDING, mf)) as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        out.append((jid, meta))
    return out


async def on_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/upload: pilih klip dari arsip buat dimasukin antrean TikTok."""
    if not is_allowed(update):
        return
    items = _pending_unqueued()
    if not items:
        await update.message.reply_text(
            "📭 Nggak ada klip yang bisa diupload (arsip kosong / semua udah antre).")
        return
    kb = []
    for jid, meta in items:
        title = (meta.get("title") or "klip").strip()[:28]
        se = f"{meta.get('start_s', '?')}-{meta.get('end_s', '?')}"
        kb.append([InlineKeyboardButton(f"⬆️ {title} ({se})",
                                       callback_data=f"ttu:{jid}")])
    if len(items) > 1:
        kb.append([InlineKeyboardButton("⬆️ Upload semua",
                                       callback_data="upall:")])
    await update.message.reply_text(
        "Pilih klip yang mau masuk antrean TikTok 👇",
        reply_markup=InlineKeyboardMarkup(kb))


TT_INTERVAL_STATE = os.environ.get("TT_INTERVAL_STATE",
                                  os.path.join(BASE, "state", "tiktok_interval.json"))


def _get_interval_state():
    """Return (interval_minutes, last_upload_epoch|None)."""
    try:
        with open(TT_INTERVAL_STATE) as f:
            d = json.load(f)
        if "interval_minutes" in d:
            return float(d["interval_minutes"]), d.get("last_upload")
        # migrasi format lama (jam -> menit)
        return float(d.get("interval_hours", 3)) * 60, d.get("last_upload")
    except Exception:
        return 180.0, None


WIB = datetime.timezone(datetime.timedelta(hours=7))


def _queue_ordered():
    """Return [jid] antrean TikTok urut TERTUA (waktu dibuat, bukan abjad).

    Job ID kita acak (uuid), jadi urutan abjad nama file TIDAK SAMA dengan
    urutan pembuatan. Pakai field `created` di job JSON (fallback: mtime).
    """
    try:
        files = [f for f in os.listdir(TT_QUEUE) if f.endswith(".json")]
    except OSError:
        return []

    def _key(f):
        p = os.path.join(TT_QUEUE, f)
        try:
            with open(p) as fh:
                created = json.load(fh).get("created")
            if created:
                return (0, float(created))
        except Exception:
            pass
        try:
            return (1, os.path.getmtime(p))
        except OSError:
            return (2, 0)

    files.sort(key=_key)
    return [f[:-5] for f in files]


def _queue_eta_map():
    """Return {jid: eta_epoch} buat job antre TikTok, urut tertua dulu.

    Estimasi: job pertama naik saat jeda terpenuhi (atau segera), tiap job
    berikutnya +1 interval. Asumsi tiap upload sukses sekali jalan.
    """
    jids = _queue_ordered()
    if not jids:
        return {}
    iv, last = _get_interval_state()
    now = time.time()
    t = (last + iv * 60) if last else now
    if t < now:
        t = now  # jeda sudah lewat -> naik di run berikutnya
    etas = {}
    for jid in jids:
        etas[jid] = t
        t += iv * 60
    return etas


def _fmt_eta(epoch):
    """Format epoch jadi 'segera' / 'HH:MM' / 'besok HH:MM' / 'dd/mm HH:MM'."""
    if epoch is None:
        return "?"
    now = time.time()
    if epoch <= now + 180:
        return "segera"
    dt = datetime.datetime.fromtimestamp(epoch, tz=WIB)
    today = datetime.datetime.now(tz=WIB).date()
    d = dt.date()
    if d == today:
        return dt.strftime("%H:%M")
    if (d - today).days == 1:
        return "besok " + dt.strftime("%H:%M")
    return dt.strftime("%d/%m %H:%M")


def _set_interval_minutes(minutes):
    last = None
    try:
        with open(TT_INTERVAL_STATE) as f:
            last = json.load(f).get("last_upload")
    except Exception:
        pass
    os.makedirs(os.path.dirname(TT_INTERVAL_STATE), exist_ok=True)
    with open(TT_INTERVAL_STATE, "w") as f:
        json.dump({"interval_minutes": minutes, "last_upload": last}, f)


async def on_jeda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/jeda [menit]: lihat/atur jeda antar upload TikTok."""
    if not is_allowed(update):
        return
    args = context.args or []
    if args:
        try:
            mnt = float(args[0].replace(",", "."))
            assert 1 <= mnt <= 10080
        except (ValueError, AssertionError):
            await update.message.reply_text(
                "Formatnya: /jeda <menit>, misal /jeda 30 (1-10080 menit).")
            return
        _set_interval_minutes(mnt)
        await update.message.reply_text(
            f"\u2705 Jeda upload TikTok diset {mnt:g} menit.\n"
            "Upload berikutnya nunggu jeda itu dari upload terakhir.")
        return
    iv, last = _get_interval_state()
    if last:
        nxt = last + iv * 60
        dt = datetime.datetime.fromtimestamp(
            nxt, tz=datetime.timezone(datetime.timedelta(hours=7)))
        info = f"Upload berikutnya bisa mulai {dt.strftime('%H:%M')} WIB."
    else:
        info = "Belum ada upload tercatat, antrean jalan langsung."
    await update.message.reply_text(
        f"\u23f1\ufe0f Jeda upload TikTok: {iv:g} menit.\n{info}\n"
        "Ganti dengan /jeda <menit>, misal /jeda 30.")


async def on_upall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Upload semua: antrekan semua klip arsip yang belum antre, caption otomatis."""
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return
    items = _pending_unqueued()
    if not items:
        await query.message.reply_text("Udah nggak ada yang bisa diupload.")
        return
    n = 0
    for jid, meta in items:
        if os.path.exists(os.path.join(TT_QUEUE, f"{jid}.json")):
            continue
        caption = build_auto_caption(meta)
        job = {
            "job_id": jid,
            "video": os.path.join(TT_PENDING, f"{jid}.mp4"),
            "caption": caption,
            "chat_id": update.effective_chat.id,
            "user_id": update.effective_user.id,
            "privacy": "public",
            "attempts": 0,
            "created": time.time(),
        }
        with open(os.path.join(TT_QUEUE, f"{jid}.json"), "w") as f:
            json.dump(job, f)
        n += 1
    try:
        await query.edit_message_text(
            f"✅ {n} klip masuk antrean upload TikTok (caption otomatis). "
            "Gue kabarin tiap udah keposting.")
    except Exception:
        await query.message.reply_text(
            f"✅ {n} klip masuk antrean upload TikTok (caption otomatis).")


def main():
    token = read_token()
    if not token:
        print("Token kosong!", file=sys.stderr)
        sys.exit(1)
    proxy = fixed_proxy(os.environ.get("HTTPS_PROXY") or
                        os.environ.get("HTTP_PROXY") or "")
    # httpx ikut baca proxy mentah dari env (trust_env) dan gagal parse
    # kredensialnya -> sembunyikan sementara saat konstruksi client saja,
    # lalu kembalikan agar subprocess yt-dlp tetap dapat proxy.
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy")
    saved = {k: os.environ.pop(k) for k in proxy_keys if k in os.environ}
    try:
        request = HTTPXRequest(proxy=proxy, connection_pool_size=8,
                               connect_timeout=30.0, read_timeout=60.0)
    finally:
        os.environ.update(saved)
    app = (ApplicationBuilder().token(token)
           .request(request).get_updates_request(request)
           .post_init(_post_init).build())

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
            CallbackQueryHandler(on_link_choice, pattern=r"^(viral|manual):"),
        ],
        states={
            ASK_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_start_time)],
            ASK_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_end_time)],
            ASK_RES: [CallbackQueryHandler(on_res, pattern=r"^res:"),
                      CallbackQueryHandler(on_subs_toggle, pattern=r"^subs:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("arsip", on_arsip))
    app.add_handler(CommandHandler("upload", on_upload))
    app.add_handler(CommandHandler("jeda", on_jeda))
    app.add_handler(CommandHandler("tag", on_tag))
    app.add_handler(CallbackQueryHandler(on_upall, pattern=r"^upall:"))
    app.add_handler(CallbackQueryHandler(on_tiktok_btn, pattern=r"^ttu:"))
    app.add_handler(CallbackQueryHandler(on_tttags, pattern=r"^tttags:"))
    app.add_handler(CallbackQueryHandler(on_ttcap, pattern=r"^ttcap:"))
    app.add_handler(CallbackQueryHandler(on_gcap, pattern=r"^gcap:"))
    app.add_handler(CallbackQueryHandler(on_gcap_mode, pattern=r"^gcapmode:"))
    app.add_handler(CallbackQueryHandler(on_del, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_qcancel, pattern=r"^qcancel:"))
    app.add_handler(CallbackQueryHandler(on_vclip, pattern=r"^vclip:"))
    app.add_handler(CallbackQueryHandler(on_vsub_toggle, pattern=r"^vsub:"))
    app.add_handler(conv)

    # retry loop: koneksi proxy kadang timeout saat init.
    # - bootstrap_retries=-1: gangguan jaringan sesaat di-retry di dalam PTB
    # - close_loop=False: loop tetap terbuka biar retry berikutnya bisa jalan
    #   (default True menutup loop -> retry selalu gagal "Event loop is closed")
    # - berhenti normal (mis. SIGTERM dari start.sh) -> break, jangan retry,
    #   biar tidak dobel polling dengan instance baru
    backoff = 5
    while True:
        try:
            log.info("ClipperBot jalan, polling...")
            app.run_polling(allowed_updates=Update.ALL_TYPES,
                            bootstrap_retries=-1,
                            close_loop=False)
            log.info("Polling berhenti normal, keluar.")
            break
        except Exception as e:
            log.warning("Polling error: %s — retry %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


if __name__ == "__main__":
    main()

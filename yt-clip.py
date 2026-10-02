#!/usr/bin/env python3
"""yt-clip.py — Potong video YouTube jadi klip 16:9.

Usage:
    yt-clip.py <url> <start> <end> [--res 720p] [--out out.mp4]

    <start>/<end> format: SS | MM:SS | HH:MM:SS   (contoh: 90, 1:00, 1:02:30)
    --res: 480p | 720p | 1080p | asli   (default: 720p)

Contoh:
    yt-clip.py "https://youtube.com/watch?v=xxx" 1:00 2:30 --res 720p
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

VENV_PY = os.environ.get("CLIPPER_VENV_PY", sys.executable)
RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}
MAX_TG_MB = 45  # batas aman di bawah limit 50MB Telegram


def parse_time(s: str) -> float:
    s = s.strip().replace(",", ".")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"Format waktu salah: {s}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


YTDLP_BASE = [VENV_PY, "-m", "yt_dlp", "--no-playlist",
              "--socket-timeout", "20",
              "--extractor-args", "youtube:player_client=android"]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def get_stream_urls(url: str, height: int):
    """Ambil direct stream URL (video+audio) via yt-dlp."""
    fmt = f"bv*[height<={height}]+ba/b[height<={height}]/b"
    r = run(YTDLP_BASE + ["-g", "-f", fmt, url], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp gagal: {r.stderr.strip()[:300]}")
    urls = [u for u in r.stdout.strip().splitlines() if u.startswith("http")]
    if not urls:
        raise RuntimeError("yt-dlp tidak mengembalikan stream URL")
    return urls


def get_title(url: str) -> str:
    # best-effort: judul cuma info tambahan, jangan pernah gugurkan klip
    try:
        r = run(YTDLP_BASE + ["--get-title", url], timeout=60)
        return r.stdout.strip()[:60] if r.returncode == 0 else "clip"
    except Exception:
        return "clip"


def parse_vtt_time(s: str) -> float:
    s = s.strip().replace(",", ".")
    total = 0.0
    for p in s.split(":"):
        total = total * 60 + float(p)
    return total


def fmt_srt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fetch_subs_vtt(url: str, workdir: str):
    """Download subtitle VTT (track id dulu, lalu en). Return path atau None."""
    r = run([VENV_PY, "-m", "yt_dlp",
             "--extractor-args", "youtube:player_client=web_embedded",
             "--write-auto-subs", "--write-subs",
             "--sub-langs", "id.*,en.*", "--sub-format", "vtt",
             "--skip-download", "--ignore-no-formats-error",
             "-o", os.path.join(workdir, "subs.%(ext)s"), url], timeout=180)
    vtts = sorted(glob.glob(os.path.join(workdir, "*.vtt")))
    if not vtts:
        return None
    for v in vtts:
        if ".id." in os.path.basename(v):
            return v
    return vtts[0]


def vtt_to_srt(vtt_path: str, start: float, end: float, srt_path: str):
    """Ambil cue dalam [start, end], geser waktunya (-start), tulis SRT.
    Return srt_path kalau ada cue, else None."""
    cue_re = re.compile(r"^((?:\d+:)?\d+:\d+[.,]\d+)\s*-->\s*((?:\d+:)?\d+:\d+[.,]\d+)")
    with open(vtt_path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    cues = []
    i = 0
    while i < len(lines):
        m = cue_re.match(lines[i].strip())
        if m:
            cs = parse_vtt_time(m.group(1))
            ce = parse_vtt_time(m.group(2))
            i += 1
            txt = []
            while i < len(lines) and lines[i].strip() \
                    and not cue_re.match(lines[i].strip()):
                t = re.sub(r"<[^>]+>", "", lines[i]).strip()
                t = (t.replace("&amp;", "&").replace("&lt;", "<")
                      .replace("&gt;", ">").replace("&quot;", '"')
                      .replace("&#39;", "'"))
                if t:
                    txt.append(t)
                i += 1
            if txt and ce > start and cs < end:
                cues.append((max(0.0, cs - start), max(0.0, ce - start), txt))
        else:
            i += 1
    # Auto-caption YouTube itu rolling: tiap cue mengulang baris sebelumnya
    # + nambah kata baru. Buang baris yang udah tampil di cue sebelumnya
    # biar subtitle cuma nunjukin omongan yang lagi jalan.
    fresh = []
    prev_lines = set()
    for (ns, ne, txt) in cues:
        new_txt = [t for t in txt if t not in prev_lines]
        prev_lines = set(txt)
        if new_txt:
            fresh.append((ns, ne, new_txt))
    ded = []
    for c in fresh:
        if not ded or ded[-1][2] != c[2]:
            ded.append(c)
    # Buang baris/cue yang cuma deskripsi suara kayak "[musik]",
    # "(tertawa)", "mendengus" — itu bukan omongan beneran.
    NOISE_WORDS = {"tertawa", "mendengus", "batuk", "bersin", "musik",
                   "tepuk tangan", "hening", "menghela napas", "hela napas",
                   "desahan", "tangisan", "teriakan"}

    def is_noise(line):
        s = line.strip().lower()
        if len(s) >= 2 and ((s[0] == "[" and s[-1] == "]") or
                            (s[0] == "(" and s[-1] == ")")):
            return True
        return s in NOISE_WORDS

    ded = [(ns, ne, [t for t in txt if not is_noise(t)])
           for (ns, ne, txt) in ded]
    ded = [c for c in ded if c[2]]
    if not ded:
        return None
    with open(srt_path, "w", encoding="utf-8") as f:
        for n, (ns, ne, txt) in enumerate(ded, 1):
            f.write(f"{n}\n{fmt_srt_time(ns)} --> {fmt_srt_time(ne)}\n"
                    + "\n".join(txt) + "\n\n")
    return srt_path


def ffmpeg_cut(inputs, dur, vf, out, maps):
    cmd = (["ffmpeg", "-y"] + inputs +
           ["-t", str(dur), "-vf", vf,
            "-c:v", "libx264", "-crf", "23", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart"] + maps + [out])
    r = run(cmd, timeout=600)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"ffmpeg gagal: {r.stderr.strip()[-300:]}")


def check_media(path):
    """Pastikan file video beneran ada isinya. Return (ok, pesan)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, "file output tidak ada"
    if size < 10_000:
        return False, f"file output kosong/rusak ({size} byte)"
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        dur = float(r.stdout.strip())
    except Exception:
        return False, "ffprobe gagal membaca output"
    if dur <= 0:
        return False, "durasi video 0 detik"
    return True, ""


def clip(url, start, end, res, out, srt_path=None):
    dur = end - start
    if dur <= 0:
        raise ValueError("Waktu selesai harus lebih besar dari waktu mulai")
    if dur > 600:
        raise ValueError("Maksimal durasi klip 10 menit")

    if res == "asli":
        vf = ("scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos,"
              "pad=ceil(iw*9/16):ih:(ow-iw)/2:0,"
              "pad=iw:ceil(ih*16/9):0:(oh-ih)/2")
        height = 720
    else:
        w, h = RESOLUTIONS[res]
        height = h
        vf = (f"scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    # Sumber cuma 360p: bersihin noise kompresi dulu (hqdn3d ringan) biar
    # nggak ikut diperbesar pas upscale, lalu lanczos + deband + unsharp
    # biar kelihatan lebih bersih & tajam. Tetap bukan HD beneran —
    # detail aslinya nggak ada.
    vf = "hqdn3d=1.5:1.5:6:6," + vf
    vf += ",deband,unsharp=5:5:0.8:5:5:0.0"

    if srt_path:
        vf += (",subtitles='" + srt_path + "'"
               ":force_style='FontName=DejaVu Sans,FontSize=20,"
               "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
               "BorderStyle=1,Outline=2,Shadow=0,MarginV=36'")

    # Jalur cepat: potong langsung dari stream URL (tanpa download full).
    # Retry 3x karena YouTube lagi agresif throttle stream (URL cepat mati).
    # Setiap tahap divalidasi: ffmpeg bisa exit 0 tapi nulis file kosong.
    RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_delay_max", "5"]
    last_err = None
    for attempt in range(3):
        try:
            urls = get_stream_urls(url, height)
            inputs = []
            for u in urls[:2]:
                inputs += RECONNECT + ["-ss", str(start), "-i", u]
            maps = ["-map", "0:v:0"]
            maps += ["-map", "1:a:0"] if len(urls) > 1 else ["-map", "0:a:0?"]
            ffmpeg_cut(inputs, dur, vf, out, maps)
            ok, why = check_media(out)
            if not ok:
                raise RuntimeError(f"stream kosong/rusak: {why}")
            return out
        except Exception as e:
            last_err = e
            if attempt < 2:
                print(f"Stream langsung gagal ({str(e)[:80]}), "
                      f"coba lagi...", flush=True)
                time.sleep(15)
            else:
                print(f"Stream langsung gagal ({str(e)[:80]}), "
                      f"download dulu...", flush=True)
    print(f"Pakai jalur download...", flush=True)

    # Jalur lambat tapi bandel: download dulu, baru potong.
    # Cuma download section yang dibutuhin (hemat, anti-macet).
    # Retry 10x dengan backoff eksponensial buat lawan throttle.
    tmp = out.replace(".mp4", "_sec.mp4")
    fmt = f"bv*[height<={height}]+ba/b[height<={height}]/b"
    sec = f"*{start}-{end}"
    r = run(YTDLP_BASE + ["-f", fmt, "--download-sections", sec,
                          "--retries", "10", "--retry-sleep", "exp=1:15",
                          "-o", tmp, url], timeout=900)
    if r.returncode != 0:
        raise RuntimeError("yt-dlp download gagal")
    ok, why = check_media(tmp)
    if not ok:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(f"download gagal (hasil kosong: {why})")
    try:
        # file section mulai dari ~0, potong durasi aja
        ffmpeg_cut(["-i", tmp], dur, vf, out, ["-map", "0:v:0", "-map", "0:a:0?"])
        ok, why = check_media(out)
        if not ok:
            raise RuntimeError(f"encode gagal: {why}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--res", default="720p",
                    choices=["480p", "720p", "1080p", "asli"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--burn-subs", action="store_true",
                    help="bakar subtitle video ke dalam klip")
    args = ap.parse_args()

    start = parse_time(args.start)
    end = parse_time(args.end)
    out = args.out or f"/tmp/clip_{os.getpid()}.mp4"

    title = get_title(args.url)
    print(f"Judul: {title}", flush=True)
    print(f"TITLE:{title.replace(chr(10), ' ')}", flush=True)
    print(f"Motong {args.start} -> {args.end} ({args.res}, 16:9)...", flush=True)

    srt_path = None
    subs_workdir = None
    if args.burn_subs:
        subs_workdir = tempfile.mkdtemp(prefix="clipsubs_")
        try:
            vtt = fetch_subs_vtt(args.url, subs_workdir)
            if vtt:
                cand = os.path.join(subs_workdir, "burn.srt")
                srt_path = vtt_to_srt(vtt, start, end, cand)
            print("Subtitle ketemu, dibakar ke video." if srt_path
                  else "Subtitle nggak ketemu, lanjut tanpa subtitle.", flush=True)
        except Exception as e:
            print(f"Ambil subtitle gagal ({str(e)[:60]}), "
                  f"lanjut tanpa subtitle.", flush=True)
    try:
        clip(args.url, start, end, args.res, out, srt_path)

        size_mb = os.path.getsize(out) / 1e6
        print(f"Jadi: {out} ({size_mb:.1f} MB)", flush=True)

        # Kalau kegedean buat Telegram, encode ulang lebih kecil
        if size_mb > MAX_TG_MB and args.res != "480p":
            print("Kegedean buat Telegram, encode ulang 480p...", flush=True)
            out2 = out.replace(".mp4", "_small.mp4")
            clip(args.url, start, end, "480p", out2, srt_path)
            size2 = os.path.getsize(out2) / 1e6
            print(f"Versi kecil: {out2} ({size2:.1f} MB)", flush=True)
            if size2 <= MAX_TG_MB:
                os.remove(out)
                out = out2
    finally:
        if subs_workdir:
            shutil.rmtree(subs_workdir, ignore_errors=True)

    ok, why = check_media(out)
    if not ok:
        raise RuntimeError(f"hasil klip rusak: {why} — coba potong ulang")

    print(f"OUTPUT:{out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

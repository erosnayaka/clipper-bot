#!/usr/bin/env python3
"""gdrive-split.py — download video dari Google Drive / MEGA, potong intro/outro,
pecah jadi klip <= chunk_len detik, kompres tiap klip biar < max_mb,
burn subtitle embedded kalau ada.

Output lines (diparse bot):
  STEP: <teks progres>
  TITLE: <judul>
  CHUNK: <path> <start_abs_detik> <end_abs_detik>
  ERROR: <pesan>

Usage:
  gdrive-split.py <drive_url> <outdir> <jobid> [--chunk 300] [--intro 90]
                  [--outro 120] [--max-mb 42] [--height 720]
"""
import argparse, json, os, re, shutil, subprocess, sys

YTDLP = [sys.executable, "-m", "yt_dlp"]
FFPROBE = ["ffprobe", "-v", "error"]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
STATE_NAME = "split_state.json"
DL_TMP = "dl_tmp"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def step(msg):
    print(f"STEP: {msg}", flush=True)


def probe(path):
    r = run(FFPROBE + ["-show_format", "-show_streams", "-of", "json", path])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe gagal: {r.stderr[:300]}")
    return json.loads(r.stdout)


def fetch_title(url):
    r = run(YTDLP + ["--no-playlist", "--skip-download",
                     "--print", "%(title)s", url])
    if r.returncode == 0:
        lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        if lines:
            return lines[-1][:80]
    return "gdrive"


def load_state(outdir):
    try:
        with open(os.path.join(outdir, STATE_NAME)) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(outdir, st):
    p = os.path.join(outdir, STATE_NAME)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def find_src(outdir):
    """Cari file sumber yang sudah terdownload (src.*) di outdir."""
    try:
        for f in os.listdir(outdir):
            if f.startswith("src.") and not f.endswith(".part") \
                    and os.path.isfile(os.path.join(outdir, f)):
                return os.path.join(outdir, f)
    except OSError:
        pass
    return None


def download_mega(url, outdir):
    """Download file publik MEGA via mega.py ke outdir/src.<ext>.
    Return (path, title)."""
    step("download dari MEGA")
    try:
        from mega import Mega
    except ImportError:
        raise RuntimeError("library mega.py belum terpasang")
    mega = Mega()
    try:
        info = mega.get_public_url_info(url)
    except Exception as e:
        raise RuntimeError(f"baca info MEGA gagal: {e}")
    info = info or {}
    title = info.get("name") or "mega"
    size = info.get("size") or 0
    if size > 2 * 1024 ** 3:
        raise RuntimeError(f"file kegedean ({size / 1e9:.1f} GB), skip")
    step(f"file: {title[:60]}")
    dl = os.path.join(outdir, DL_TMP)
    shutil.rmtree(dl, ignore_errors=True)  # mega.py tidak bisa resume
    os.makedirs(dl, exist_ok=True)
    try:
        path = mega.download_url(url, dest_path=dl)
    except Exception as e:
        msg = str(e)
        if "509" in msg or "overquota" in msg.lower():
            raise RuntimeError("kuota download MEGA habis, coba lagi nanti")
        raise RuntimeError(f"download MEGA gagal: {msg[:200]}")
    if not path or not os.path.exists(path):
        cands = [os.path.join(dl, f) for f in os.listdir(dl)
                 if os.path.isfile(os.path.join(dl, f))]
        if not cands:
            raise RuntimeError("file hasil download MEGA tidak ketemu")
        path = max(cands, key=os.path.getmtime)
    ext = os.path.splitext(path)[1] or ".mp4"
    final = os.path.join(outdir, f"src{ext}")
    if os.path.exists(final):
        os.remove(final)
    shutil.move(path, final)
    return final, title


def download(url, outdir):
    """Download ke outdir/src.<ext>. yt-dlp otomatis resume file .part."""
    if re.search(r"mega" + r"\.nz", url):
        return download_mega(url, outdir)
    step("download dari Google Drive")
    title = fetch_title(url)
    dl = os.path.join(outdir, DL_TMP)
    os.makedirs(dl, exist_ok=True)
    out = os.path.join(dl, "src.%(ext)s")
    r = subprocess.run(
        YTDLP + ["--no-playlist", "-o", out, url],
        capture_output=False, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("yt-dlp download gagal")
    files = [f for f in os.listdir(dl) if f.startswith("src.")
             and not f.endswith(".part")]
    if not files:
        raise RuntimeError("file hasil download tidak ketemu")
    got = os.path.join(dl, files[0])
    ext = os.path.splitext(files[0])[1] or ".mp4"
    final = os.path.join(outdir, f"src{ext}")
    if os.path.exists(final):
        os.remove(final)
    shutil.move(got, final)
    return final, title


def extract_subs(src, outdir):
    """Ambil subtitle embedded pertama -> outdir/subs.srt. Return path/None."""
    info = probe(src)
    subs = [s for s in info["streams"] if s.get("codec_type") == "subtitle"]
    if not subs:
        return None
    step("subtitle ketemu, diekstrak")
    out = os.path.join(outdir, "subs.srt")
    r = run(["ffmpeg", "-y", "-v", "error", "-i", src, "-map", "0:s:0", out])
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 50:
        return None
    return out


def parse_srt(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        m = re.match(
            r"(\d+):(\d+):([\d.,]+)\s*-->\s*(\d+):(\d+):([\d.,]+)", lines[1])
        if not m:
            continue
        def ts(h, mnt, s):
            return int(h) * 3600 + int(mnt) * 60 + float(s.replace(",", "."))
        cues.append((ts(*m.groups()[:3]), ts(*m.groups()[3:6]),
                      "\n".join(lines[2:])))
    return cues


def fmt_ts(sec):
    ms = int(round((sec - int(sec)) * 1000))
    h, sec = divmod(int(sec), 3600)
    m, s = divmod(sec, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_chunk_srt(cues, start, end, path):
    kept = []
    for i, (cs, ce, txt) in enumerate(cues):
        if ce <= start or cs >= end:
            continue
        kept.append((max(cs, start) - start, min(ce, end) - start, txt))
    with open(path, "w", encoding="utf-8") as f:
        for i, (cs, ce, txt) in enumerate(kept, 1):
            f.write(f"{i}\n{fmt_ts(cs)} --> {fmt_ts(ce)}\n{txt}\n\n")
    return len(kept)


def split_ranges(content_start, content_end, chunk_len):
    """Bagi jadi potongan <= chunk_len; ekor < 60 d digabung ke potongan sblmnya."""
    total = content_end - content_start
    ranges = []
    t = content_start
    while t < content_end:
        d = min(chunk_len, content_end - t)
        ranges.append((t, t + d))
        t += d
    if len(ranges) > 1 and (ranges[-1][1] - ranges[-1][0]) < 60:
        ps, _ = ranges[-2]
        ranges[-2] = (ps, ranges[-1][1])
        ranges.pop()
    return ranges


def encode_chunk(src, srt_path, start, dur, out, height, max_mb):
    total_kbps = max_mb * 8192 / dur
    abit = 96
    vbit = max(400, int(total_kbps - abit))
    vf = (f"scale=-2:{height}:flags=lanczos,"
          f"pad=ceil(iw/2)*2:ceil(ih/2)*2")
    filt = vf
    if srt_path and os.path.exists(srt_path):
        esc = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "")
        filt += (f",subtitles='{esc}':fontsdir='/usr/share/fonts/truetype/dejavu'"
                 f":force_style='FontName=DejaVu Sans,FontSize=16,"
                 f"PrimaryColour=&HFFFFFF,OutlineColour=&H80000000,"
                 f"BorderStyle=1,Outline=1,Shadow=0'")
    cmd = (["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}",
            "-t", f"{dur:.3f}", "-i", src,
            "-vf", filt,
            "-c:v", "libx264", "-preset", "fast",
            "-b:v", f"{vbit}k", "-maxrate", f"{int(vbit*1.5)}k",
            "-bufsize", f"{int(vbit*2)}k", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{abit}k",
            "-movflags", "+faststart", out])
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg gagal: {r.stderr[:300]}")


def valid(path):
    if not os.path.exists(path) or os.path.getsize(path) < 10 * 1024:
        return False
    try:
        info = probe(path)
        return float(info["format"].get("duration", 0)) > 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("outdir")
    ap.add_argument("jobid")
    ap.add_argument("--chunk", type=float, default=300)
    ap.add_argument("--intro", type=float, default=90)
    ap.add_argument("--outro", type=float, default=120)
    ap.add_argument("--max-mb", type=float, default=42)
    ap.add_argument("--height", type=int, default=720)
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    params = {"url": a.url, "intro": a.intro, "outro": a.outro,
              "chunk": a.chunk, "max_mb": a.max_mb, "height": a.height}
    st = load_state(a.outdir)
    resume = bool(st) and all(st.get(k) == v for k, v in params.items())
    try:
        if resume and not valid(st.get("src_file") or ""):
            step("file sumber rusak, download ulang")
            resume = False
        if not resume:
            st = dict(params)
            st.update({"done": [], "title": "", "dur": 0, "ranges": [],
                       "src_file": ""})
            src = find_src(a.outdir)
            if src and not valid(src):
                os.remove(src)
                src = None
            if not src:
                src, raw_title = download(a.url, a.outdir)
                st["title"] = raw_title
            elif not st.get("title"):
                st["title"] = fetch_title(a.url)
            st["src_file"] = src
            save_state(a.outdir, st)
            src = st["src_file"]
        else:
            step("lanjut dari proses sebelumnya (resume)")
            src = st["src_file"]

        if not resume or not st.get("ranges"):
            info = probe(src)
            dur = float(info["format"]["duration"])
            title = st.get("title") or "gdrive"
            print(f"TITLE: {title}", flush=True)

            cs, ce = a.intro, dur - a.outro
            if ce - cs < 60:
                raise RuntimeError(f"video terlalu pendek ({dur:.0f}s) buat dipotong intro/outro")
            step(f"durasi {dur:.0f}s -> konten {cs:.0f}s-{ce:.0f}s (intro/outro dibuang)")

            srt = extract_subs(src, a.outdir)
            cues = parse_srt(srt) if srt else []
            if cues:
                step(f"{len(cues)} baris subtitle siap diburn")

            st["dur"] = dur
            st["ranges"] = split_ranges(cs, ce, a.chunk)
            save_state(a.outdir, st)
        else:
            title = st.get("title") or "gdrive"
            print(f"TITLE: {title}", flush=True)
            srt = os.path.join(a.outdir, "subs.srt")
            cues = parse_srt(srt) if os.path.exists(srt) else []
            # kabari lagi chunk yang sudah jadi (biar bot bisa rebuild daftar)
            for i in list(st["done"]):
                s, e = st["ranges"][i]
                out = os.path.join(a.outdir, f"{a.jobid}_p{i + 1}.mp4")
                if valid(out):
                    mb = os.path.getsize(out) / 1024 / 1024
                    print(f"CHUNK: {out} {s:.1f} {e:.1f} {mb:.1f}MB",
                          flush=True)
                else:
                    st["done"].remove(i)
            save_state(a.outdir, st)

        ranges = st["ranges"]
        done = set(st["done"])
        step(f"jadi {len(ranges)} klip")
        for i, (s, e) in enumerate(ranges):
            if i in done:
                continue
            step(f"klip {i + 1}/{len(ranges)} ({s / 60:.0f}:{s % 60:04.1f})")
            chunk_srt = None
            if cues:
                chunk_srt = os.path.join(a.outdir, f"c{i + 1}.srt")
                n = write_chunk_srt(cues, s, e, chunk_srt)
                if n == 0:
                    chunk_srt = None
            out = os.path.join(a.outdir, f"{a.jobid}_p{i + 1}.mp4")
            encode_chunk(src, chunk_srt, s, e - s, out, a.height, a.max_mb)
            if not valid(out):
                raise RuntimeError(f"klip {i + 1} gagal validasi")
            mb = os.path.getsize(out) / 1024 / 1024
            print(f"CHUNK: {out} {s:.1f} {e:.1f} {mb:.1f}MB", flush=True)
            done.add(i)
            st["done"] = sorted(done)
            save_state(a.outdir, st)
        print(f"DONE: {len(ranges)}", flush=True)
        step("selesai")
    except Exception as ex:
        print(f"ERROR: {ex}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

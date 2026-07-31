#!/usr/bin/env python3
"""
Stag Do Bingo — montage builder.

Reads every piece of evidence from Supabase, compresses and trims each
clip to a few seconds, puts a caption card with the challenge text before
each one, stitches the lot into a single montage, and uploads the finished
video back to the bucket as montage/stag-montage.mp4.

Runs on GitHub Actions (no local install needed). Needs three secrets:
  SUPABASE_URL, SUPABASE_SERVICE_KEY, and reads config from the DB.

Tunables are at the top of main().
"""

import os
import re
import sys
import json
import shutil
import textwrap
import subprocess
from pathlib import Path
from urllib.request import urlopen, Request

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET = os.environ.get("BUCKET", "stag-evidence")

WORK = Path("montage_work")
CLIPS = WORK / "clips"
DL = WORK / "downloads"
for d in (CLIPS, DL):
    d.mkdir(parents=True, exist_ok=True)


# ---------- small Supabase REST helpers (no SDK needed) ----------
def _headers(extra=None):
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    if extra:
        h.update(extra)
    return h


def db_select(table, columns="*", order=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}?select={columns}"
    if order:
        url += f"&order={order}"
    req = Request(url, headers=_headers())
    with urlopen(req) as r:
        return json.loads(r.read().decode())


def storage_download(path, dest):
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    req = Request(url, headers=_headers())
    with urlopen(req) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def storage_upload(path, src, content_type="video/mp4"):
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"
    data = open(src, "rb").read()
    # upsert so re-runs overwrite the previous montage
    req = Request(url, data=data, method="POST",
                  headers=_headers({"Content-Type": content_type, "x-upsert": "true"}))
    with urlopen(req) as r:
        return r.status


# ---------- ffmpeg helpers ----------
TARGET_W, TARGET_H = 720, 1280   # portrait 720p — good for phones, small files
FPS = 30


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_capture(cmd):
    """Like run() but on failure the CalledProcessError carries stderr."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, p.stdout, p.stderr)


def probe_duration(src):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def caption_card(text, out, seconds=2.5):
    """A dark navy card with centred challenge text and scattered confetti."""
    wrapped = "\n".join(textwrap.wrap(text, width=16)) or " "
    tf = WORK / "cap.txt"
    tf.write_text(wrapped, encoding="utf-8")

    # scatter confetti: small coloured rectangles at pseudo-random spots
    import random
    rnd = random.Random(hash(text) & 0xFFFF)   # same text -> same layout
    colours = ["0xC7CFDA", "0xFFFFFF", "0x4A6FA5", "0x8FA8CC"]
    draws = []
    for _ in range(28):
        x = rnd.randint(20, TARGET_W - 40)
        y = rnd.randint(20, TARGET_H - 40)
        w = rnd.randint(10, 26)
        h = rnd.randint(6, 14)
        c = rnd.choice(colours)
        # keep confetti out of the central text band so it stays readable
        if TARGET_H * 0.38 < y < TARGET_H * 0.62:
            y = y - int(TARGET_H * 0.30) if y < TARGET_H * 0.5 else y + int(TARGET_H * 0.30)
        draws.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={c}@0.9:t=fill")

    confetti = ",".join(draws)
    textfilter = (
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"textfile={tf}:fontcolor=0xF4F6F9:fontsize=48:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=16:text_align=center"
    )

    run_capture([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x121E33:s={TARGET_W}x{TARGET_H}:d={seconds}:r={FPS}",
        "-vf", f"{confetti},{textfilter}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(seconds),
        str(out),
    ])


def normalise_image(src, out, seconds=3.0):
    """Still photo -> a few seconds of silent video at target size."""
    run_capture([
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(seconds), "-i", str(src),
        "-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-vf", (f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
                f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS}"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
        "-shortest", str(out),
    ])


def normalise_video(src, out, clip_len):
    """Video -> trimmed, compressed, target size, with audio."""
    run_capture([
        "ffmpeg", "-y", "-i", str(src), "-t", str(clip_len),
        "-vf", (f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
                f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"),
        "-r", str(FPS),
        "-c:v", "libx264", "-crf", "26", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
        str(out),
    ])
    # if the source had no audio, the above can fail; caller handles fallback


def normalise_video_safe(src, out, clip_len):
    try:
        normalise_video(src, out, clip_len)
    except subprocess.CalledProcessError:
        # add a silent track then retry (covers clips with no audio stream)
        run_capture([
            "ffmpeg", "-y", "-i", str(src),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(clip_len),
            "-vf", (f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
                    f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"),
            "-r", str(FPS),
            "-c:v", "libx264", "-crf", "26", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100", "-shortest",
            str(out),
        ])


# ---------- main ----------
def main():
    CLIP_MIN, CLIP_MAX = 5, 10   # seconds per video clip
    PHOTO_SECS = 2               # seconds per photo
    CARD_SECS = 2.5              # seconds per caption card

    print("Loading config + evidence from Supabase…")
    cfg = db_select("config", "stag_name,event_name,activities")[0]
    activities = cfg["activities"]
    stag = cfg["stag_name"]
    bonuses = {b["line_key"]: b["challenge"] for b in db_select("bonuses", "line_key,challenge")}
    evidence = db_select("evidence", "id,square_id,line_key,path,media_type", order="created_at")

    if not evidence:
        print("No evidence found — nothing to build.")
        sys.exit(0)

    def label_for(e):
        if e["square_id"] is not None:
            return activities[e["square_id"]] if e["square_id"] < len(activities) else "Challenge"
        return "Bonus — " + bonuses.get(e["line_key"], "")

    segments = []
    idx = 0

    # opening title
    title_card = CLIPS / "000_title.mp4"
    caption_card(f"{stag}\n\nThe Evidence", title_card, seconds=3.0)
    segments.append(title_card)

    for e in evidence:
        idx += 1
        label = label_for(e)
        print(f"[{idx}/{len(evidence)}] {label}")

        # caption card before the media
        card = CLIPS / f"{idx:03d}_card.mp4"
        caption_card(label, card, seconds=CARD_SECS)
        segments.append(card)

        # download the source file
        ext = Path(e["path"]).suffix or ".bin"
        src = DL / f"{idx:03d}{ext}"
        try:
            storage_download(e["path"], src)
        except Exception as ex:
            print(f"  ! couldn't download {e['path']}: {ex}")
            continue

        seg = CLIPS / f"{idx:03d}_media.mp4"
        try:
            if e["media_type"] == "video":
                dur = probe_duration(src)
                clip_len = max(CLIP_MIN, min(CLIP_MAX, dur if dur else CLIP_MAX))
                normalise_video_safe(src, seg, clip_len)
            else:
                normalise_image(src, seg, seconds=PHOTO_SECS)
            segments.append(seg)
        except subprocess.CalledProcessError as ex:
            err = (ex.stderr or b"").decode(errors="replace")[-500:]
            print(f"  ! ffmpeg failed on this item ({e['media_type']}), skipping.")
            print(f"    reason: {err}")
            continue

    # closing card
    end_card = CLIPS / "999_end.mp4"
    caption_card("FULL HOUSE\n\nWhat happens on the stag…", end_card, seconds=3.0)
    segments.append(end_card)

    # concat everything
    print(f"Stitching {len(segments)} segments…")
    listfile = WORK / "concat.txt"
    listfile.write_text("".join(f"file '{s.resolve()}'\n" for s in segments), encoding="utf-8")
    out = WORK / "stag-montage.mp4"
    run_capture([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
        "-c:v", "libx264", "-crf", "24", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", str(out),
    ])

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"Montage built: {size_mb:.1f} MB. Uploading…")
    storage_upload("montage/stag-montage.mp4", out)
    print("Done. Find it in the bucket at montage/stag-montage.mp4")


if __name__ == "__main__":
    main()
  

"""Daily "Today in History" Short generator (v2).

Pipeline: Wikipedia "selected" event (favoring notable ones with images)
-> script from Wikipedia text only -> edge-tts voice -> zooming image
backgrounds with captions -> ffmpeg video -> optional YouTube upload.
"""
import asyncio
import datetime
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

UA = {"User-Agent": "TodayInHistoryBot/2.0 (contact: eviltieempire@gmail.com)"}  # put your email
W, H = 1080, 1920
FPS = 30
OUT = Path("out")
OUT.mkdir(exist_ok=True)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
VOICE = "en-US-GuyNeural"

# Skip sensitive events (demonetization risk). Tweak to taste.
SKIP = re.compile(
    r"\b(kill|massacre|murder|assassinat|shoot|bomb|genocide|suicide|rape|"
    r"terror|execut|died|death|dead|attack|slaughter|torture)\w*",
    re.I,
)


def get_json(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def has_image(page):
    return bool(page.get("originalimage") or page.get("thumbnail"))


def fetch_event(d):
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{d:%m}/{d:%d}"
    items = [
        e for e in get_json(url).get("selected", [])
        if e.get("pages") and not SKIP.search(e["text"])
    ]
    if not items:
        sys.exit("No suitable events today.")
    # Prefer events with images and many linked pages (a rough notability signal).
    items.sort(
        key=lambda e: (any(has_image(p) for p in e["pages"]), min(len(e["pages"]), 5)),
        reverse=True,
    )
    top = items[:3]
    return top[d.year % len(top)]


def first_sentence(page):
    """First sentence of the linked page's Wikipedia summary, or None."""
    try:
        title = (page.get("titles") or {}).get("canonical") or page["title"]
        data = get_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                        + requests.utils.quote(title.replace(" ", "_"), safe=""))
        text = data.get("extract", "")
    except Exception:
        return None
    text = re.sub(r"\s*\([^)]*\)", "", text)  # drop pronunciations, date ranges
    m = re.match(r"(.+?[.!?])(\s+[A-Z]|$)", text)
    if not m:
        return None
    s = m.group(1).strip()
    if not 40 <= len(s) <= 260 or SKIP.search(s):
        return None
    return s


def build_lines(d, ev):
    text = ev["text"].strip().rstrip(".") + "."
    lines = [f"{d:%B} {d.day}, {ev['year']}.", text]
    extra = first_sentence(ev["pages"][0])
    if extra and extra.lower()[:30] not in text.lower():
        lines.append(extra)
    lines.append("Follow for tomorrow's story.")
    return lines


def get_images(ev, limit=3):
    paths = []
    for page in ev["pages"]:
        if len(paths) >= limit:
            break
        img = page.get("originalimage") or page.get("thumbnail")
        if not img or not img["source"].lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        try:
            r = requests.get(img["source"], headers=UA, timeout=60)
            r.raise_for_status()
            p = OUT / f"img{len(paths)}.img"
            p.write_bytes(r.content)
            Image.open(p).verify()
            paths.append(p)
        except Exception:
            continue
    return paths


def make_background(path, out):
    """Darkened, cropped 1.5x-size background (extra size gives room to zoom)."""
    bw, bh = int(W * 1.5), int(H * 1.5)
    if path:
        img = Image.open(path).convert("RGB")
        scale = max(bw / img.width, bh / img.height)
        img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
        left, top = (img.width - bw) // 2, (img.height - bh) // 2
        img = img.crop((left, top, left + bw, top + bh))
        img = ImageEnhance.Brightness(img).enhance(0.45)
    else:  # vertical gradient fallback
        img = Image.new("RGB", (bw, bh))
        dr = ImageDraw.Draw(img)
        for y in range(bh):
            t = y / bh
            dr.line([(0, y), (bw, y)], fill=(int(20 + 40 * t), int(28 + 10 * t), int(70 - 30 * t)))
    img.save(out)


def make_overlay(year, caption, out):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.text((W // 2, 200), "ON THIS DAY", font=ImageFont.truetype(FONT, 56),
            fill=(225, 225, 225, 255), anchor="mm", stroke_width=3, stroke_fill="black")
    dr.text((W // 2, 330), str(year), font=ImageFont.truetype(FONT, 200),
            fill="white", anchor="mm", stroke_width=5, stroke_fill="black")
    n = len(caption)
    size = 72 if n < 120 else 58 if n < 220 else 48
    wrapped = "\n".join(textwrap.wrap(caption, width=int(900 / (size * 0.58))))
    dr.multiline_text((W // 2, 1100), wrapped, font=ImageFont.truetype(FONT, size),
                      fill="white", anchor="mm", align="center",
                      stroke_width=5, stroke_fill="black", spacing=18)
    img.save(out)


async def _tts(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))


def tts(text, path):
    for attempt in range(3):
        try:
            asyncio.run(_tts(text, path))
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def render(lines, year, images):
    segs = []
    for i, line in enumerate(lines):
        audio, seg = OUT / f"l{i}.mp3", OUT / f"s{i}.mp4"
        bg, ov = OUT / f"bg{i}.png", OUT / f"ov{i}.png"
        tts(line, audio)
        dur = duration(audio) + 0.3
        frames = int(dur * FPS) + 1
        make_background(images[min(i, len(images) - 1)] if images else None, bg)
        make_overlay(year, line.rstrip("."), ov)
        zoom = (f"zoompan=z='min(1+0.0007*on,1.2)':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS}")
        run(["ffmpeg", "-y",
             "-i", str(bg),
             "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.2f}", "-i", str(ov),
             "-i", str(audio),
             "-filter_complex",
             f"[0:v]{zoom}[z];[z][1:v]overlay=0:0:shortest=1,format=yuv420p[v]",
             "-map", "[v]", "-map", "2:a", "-af", f"apad=whole_dur={dur:.2f}",
             "-t", f"{dur:.2f}", "-c:v", "libx264", "-r", str(FPS),
             "-c:a", "aac", "-ar", "44100", "-ac", "2", str(seg)])
        segs.append(seg)
    lst = OUT / "list.txt"
    lst.write_text("".join(f"file '{s.name}'\n" for s in segs))
    final = OUT / "video.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(final)])
    return final


def upload(video, title, desc):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": title[:100], "description": desc, "categoryId": "27"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    yt.videos().insert(part="snippet,status", body=body,
                       media_body=MediaFileUpload(str(video))).execute()


def main():
    d = datetime.date.today()
    ev = fetch_event(d)
    lines = build_lines(d, ev)
    images = get_images(ev)
    video = render(lines, ev["year"], images)

    page = ev["pages"][0]
    page_url = page["content_urls"]["desktop"]["page"]
    title = f"{d:%B} {d.day}, {ev['year']}: {ev['text'][:60].rstrip()}... #Shorts"
    desc = (f"{lines[0]} {lines[1]}\n\nSource: Wikipedia ({page_url}), "
            f"CC BY-SA 4.0. Images from Wikipedia/Wikimedia Commons; see the "
            f"article for credits.\n\n#shorts #history #todayinhistory")
    (OUT / "description.txt").write_text(title + "\n\n" + desc + "\n\n" + "\n".join(lines))

    if os.environ.get("YT_REFRESH_TOKEN"):
        upload(video, title, desc)
        print("Uploaded.")
    else:
        print("Built", video, "(no YouTube credentials set, skipping upload)")


if __name__ == "__main__":
    main()

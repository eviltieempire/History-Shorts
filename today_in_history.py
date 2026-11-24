"""Daily "Today in History" Short generator.

Pipeline: Wikipedia "selected" event -> script (verbatim from source)
-> edge-tts voice -> image frames with captions -> ffmpeg video -> YouTube upload.
Needs: ffmpeg, Pillow, requests, edge-tts (+ google libs for upload).
"""
import asyncio
import datetime
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

UA = {"User-Agent": "TodayInHistoryBot/1.0 (contact: eviltieempire@gmail.com)"}  # put your email
W, H = 1080, 1920
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


def fetch_event(d):
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{d:%m}/{d:%d}"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    items = [
        e for e in r.json().get("selected", [])
        if e.get("pages") and not SKIP.search(e["text"])
    ]
    if not items:
        sys.exit("No suitable events today.")
    items.sort(key=lambda e: e["year"])
    return items[d.year % len(items)]


def build_lines(d, ev):
    text = ev["text"].strip().rstrip(".") + "."
    return [f"{d:%B} {d.day}, {ev['year']}.", text, "Follow for tomorrow's story."]


def get_image(ev):
    for page in ev["pages"]:
        img = page.get("originalimage") or page.get("thumbnail")
        if not img:
            continue
        src = img["source"]
        if not src.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        try:
            r = requests.get(src, headers=UA, timeout=60)
            r.raise_for_status()
            path = OUT / "bg.img"
            path.write_bytes(r.content)
            Image.open(path).verify()
            return path, page
        except Exception:
            continue
    return None, ev["pages"][0]


def make_frame(bg_path, year, caption, out):
    if bg_path:
        img = Image.open(bg_path).convert("RGB")
        scale = max(W / img.width, H / img.height)
        img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
        left, top = (img.width - W) // 2, (img.height - H) // 2
        img = img.crop((left, top, left + W, top + H))
    else:
        img = Image.new("RGB", (W, H), (18, 24, 48))
    img = ImageEnhance.Brightness(img).enhance(0.4)
    dr = ImageDraw.Draw(img)

    dr.text((W // 2, 200), "ON THIS DAY", font=ImageFont.truetype(FONT, 56),
            fill=(220, 220, 220), anchor="mm")
    dr.text((W // 2, 330), str(year), font=ImageFont.truetype(FONT, 200),
            fill="white", anchor="mm")

    n = len(caption)
    size = 72 if n < 120 else 58 if n < 220 else 48
    wrapped = "\n".join(textwrap.wrap(caption, width=int(900 / (size * 0.58))))
    dr.multiline_text((W // 2, 1050), wrapped, font=ImageFont.truetype(FONT, size),
                      fill="white", anchor="mm", align="center",
                      stroke_width=4, stroke_fill="black", spacing=18)
    img.save(out)


async def tts(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def render(lines, year, bg_path):
    segs = []
    for i, line in enumerate(lines):
        audio, frame, seg = OUT / f"l{i}.mp3", OUT / f"f{i}.png", OUT / f"s{i}.mp4"
        asyncio.run(tts(line, audio))
        make_frame(bg_path, year, line.rstrip("."), frame)
        run(["ffmpeg", "-y", "-loop", "1", "-i", str(frame), "-i", str(audio),
             "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
             "-r", "30", "-c:a", "aac", "-ar", "44100", "-ac", "2",
             "-shortest", str(seg)])
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
    bg, page = get_image(ev)
    video = render(lines, ev["year"], bg)

    page_url = page["content_urls"]["desktop"]["page"]
    title = f"{d:%B} {d.day}, {ev['year']}: {ev['text'][:60].rstrip()}... #Shorts"
    desc = (f"{lines[0]} {lines[1]}\n\nSource: Wikipedia ({page_url}), "
            f"CC BY-SA 4.0. Images from Wikipedia/Wikimedia Commons; see page for credits.\n\n"
            f"#shorts #history #todayinhistory")
    (OUT / "description.txt").write_text(title + "\n\n" + desc)

    if os.environ.get("YT_REFRESH_TOKEN"):
        upload(video, title, desc)
        print("Uploaded.")
    else:
        print("Built", video, "(no YouTube credentials set, skipping upload)")


if __name__ == "__main__":
    main()

import modal
import os
import uuid
import json
import base64
import tempfile
from pathlib import Path

app = modal.App("arabic-lecture-player")

image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "yt-dlp",
    "openai",
    "anthropic",
    "fastapi",
    "uvicorn",
).apt_install("ffmpeg")

volume = modal.Volume.from_name("arabic-lecture-storage", create_if_missing=True)
STORAGE_PATH = "/storage"

@app.function(
    image=image,
    timeout=600,
    volumes={STORAGE_PATH: volume},
    secrets=[
        modal.Secret.from_name("openai-secret"),
        modal.Secret.from_name("anthropic-secret"),
    ],
)
def process_lecture(job_id: str, youtube_url: str):
    import yt_dlp
    from openai import OpenAI
    import anthropic

    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    status_path.write_text(json.dumps({"status": "processing", "step": "downloading"}))
    volume.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "audio.mp3")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
            "quiet": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            title = info.get("title", "Arabic Lecture")

        # find the mp3
        for f in os.listdir(tmpdir):
            if f.endswith(".mp3"):
                audio_path = os.path.join(tmpdir, f)
                break

        status_path.write_text(json.dumps({"status": "processing", "step": "transcribing"}))
        volume.commit()

        with open(audio_path, "rb") as af:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=af,
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
                language="ar",
            )

        segments = transcript.segments
        words = transcript.words if hasattr(transcript, "words") and transcript.words else []

        status_path.write_text(json.dumps({"status": "processing", "step": "translating"}))
        volume.commit()

        translated_segments = []
        for seg in segments:
            response = anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": f"Translate this Arabic text to clear English for general comprehension. Keep it natural and readable. Return only the translation, nothing else.\n\n{seg.text}"
                }]
            )
            translation = response.content[0].text.strip()
            translated_segments.append({
                "id": seg.id,
                "start": seg.start,
                "end": seg.end,
                "arabic": seg.text.strip(),
                "english": translation,
            })

        # build word-start lookup keyed by segment
        word_map = {}
        if words:
            for w in words:
                word_map[round(w.start, 3)] = w.word

        status_path.write_text(json.dumps({"status": "processing", "step": "building_html"}))
        volume.commit()

        with open(audio_path, "rb") as af:
            audio_b64 = base64.b64encode(af.read()).decode("utf-8")

        html = _build_html(title, audio_b64, translated_segments, words)

        html_path = Path(STORAGE_PATH) / f"{job_id}.html"
        html_path.write_text(html, encoding="utf-8")

        status_path.write_text(json.dumps({
            "status": "done",
            "html_filename": f"{job_id}.html",
        }))
        volume.commit()


def _build_html(title: str, audio_b64: str, segments: list, words: list) -> str:
    segments_json = json.dumps(segments, ensure_ascii=False)
    words_json = json.dumps(
        [{"start": w.start, "end": w.end, "word": w.word} for w in words] if words else [],
        ensure_ascii=False,
    )

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0f0f0f;
    color: #e0e0e0;
    font-family: 'Segoe UI', Arial, sans-serif;
    min-height: 100vh;
  }}
  header {{
    background: #1a1a1a;
    padding: 16px;
    position: sticky;
    top: 0;
    z-index: 10;
    box-shadow: 0 2px 8px rgba(0,0,0,0.5);
  }}
  h1 {{
    font-size: 1rem;
    color: #fff;
    text-align: center;
    direction: ltr;
    margin-bottom: 12px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  audio {{
    width: 100%;
    height: 40px;
    accent-color: #4fa3e0;
  }}
  #transcript {{
    padding: 12px;
    max-width: 800px;
    margin: 0 auto;
  }}
  .segment {{
    background: #1c1c1c;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 10px;
    border-right: 3px solid transparent;
    transition: border-color 0.2s, background 0.2s;
    cursor: pointer;
  }}
  .segment.active {{
    background: #1e2d3d;
    border-right-color: #4fa3e0;
  }}
  .arabic {{
    font-size: 1.35rem;
    line-height: 1.7;
    color: #f0f0f0;
    text-align: right;
    direction: rtl;
    font-family: 'Amiri', 'Traditional Arabic', serif;
  }}
  .english {{
    font-size: 0.88rem;
    color: #888;
    margin-top: 8px;
    text-align: left;
    direction: ltr;
    line-height: 1.5;
  }}
  .timestamp {{
    font-size: 0.72rem;
    color: #555;
    text-align: left;
    direction: ltr;
    margin-top: 4px;
  }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <audio id="player" controls>
    <source src="data:audio/mp3;base64,{audio_b64}" type="audio/mp3">
  </audio>
</header>
<div id="transcript"></div>
<script>
const segments = {segments_json};
const words = {words_json};

const player = document.getElementById('player');
const container = document.getElementById('transcript');

function fmt(t) {{
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60).toString().padStart(2, '0');
  return `${{m}}:${{s}}`;
}}

segments.forEach((seg, i) => {{
  const div = document.createElement('div');
  div.className = 'segment';
  div.id = `seg-${{i}}`;
  div.innerHTML = `
    <div class="arabic">${{seg.arabic}}</div>
    <div class="english">${{seg.english}}</div>
    <div class="timestamp">${{fmt(seg.start)}} – ${{fmt(seg.end)}}</div>
  `;
  div.addEventListener('click', () => {{ player.currentTime = seg.start; player.play(); }});
  container.appendChild(div);
}});

let lastActive = -1;
player.addEventListener('timeupdate', () => {{
  const t = player.currentTime;
  let active = -1;
  for (let i = 0; i < segments.length; i++) {{
    if (t >= segments[i].start && t < segments[i].end) {{ active = i; break; }}
  }}
  if (active === lastActive) return;
  if (lastActive >= 0) document.getElementById(`seg-${{lastActive}}`).classList.remove('active');
  if (active >= 0) {{
    const el = document.getElementById(`seg-${{active}}`);
    el.classList.add('active');
    el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
  }}
  lastActive = active;
}});
</script>
</body>
</html>"""


@app.function(
    image=image,
    volumes={STORAGE_PATH: volume},
)
@modal.fastapi_endpoint(method="POST", label="process")
def process_endpoint(body: dict):
    youtube_url = body.get("youtube_url", "").strip()
    if not youtube_url:
        return {"error": "youtube_url is required"}, 400
    job_id = str(uuid.uuid4())
    process_lecture.spawn(job_id, youtube_url)
    return {"status": "processing", "job_id": job_id}


@app.function(
    image=image,
    volumes={STORAGE_PATH: volume},
)
@modal.fastapi_endpoint(method="GET", label="status")
def status_endpoint(job_id: str):
    volume.reload()
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    if not status_path.exists():
        return {"status": "not_found"}

    data = json.loads(status_path.read_text())
    if data.get("status") == "done":
        download_url = f"https://mahdid313--download.modal.run?job_id={job_id}"
        return {"status": "done", "download_url": download_url}

    return {"status": data.get("status", "processing"), "step": data.get("step", "")}


@app.function(
    image=image,
    volumes={STORAGE_PATH: volume},
)
@modal.fastapi_endpoint(method="GET", label="download")
def download_endpoint(job_id: str):
    from fastapi.responses import HTMLResponse
    volume.reload()
    html_path = Path(STORAGE_PATH) / f"{job_id}.html"
    if not html_path.exists():
        return {"error": "file not found"}
    content = html_path.read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={
        "Content-Disposition": f'attachment; filename="lecture-{job_id[:8]}.html"'
    })

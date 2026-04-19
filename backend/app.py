import modal
import os
import uuid
import json
import base64
import tempfile
from pathlib import Path

app = modal.App("arabic-lecture-player")

image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "yt-dlp==2026.3.17",
    "openai",
    "anthropic",
    "fastapi",
    "uvicorn",
    "httpx",
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
        modal.Secret.from_name("youtube-cookies"),
    ],
)
def process_lecture(job_id: str, youtube_url: str):
    import time
    import traceback
    import yt_dlp
    from openai import OpenAI
    import anthropic

    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    logs = []
    start_time = time.time()

    def fail(error_msg: str):
        logs.append({"t": round(time.time() - start_time, 1), "msg": f"FAILED: {error_msg}"})
        status_path.write_text(json.dumps({
            "status": "failed",
            "error": error_msg,
            "logs": logs,
        }, ensure_ascii=False))
        volume.commit()

    def update(step: str, message: str):
        elapsed = round(time.time() - start_time, 1)
        logs.append({"t": elapsed, "msg": message})
        status_path.write_text(json.dumps({
            "status": "processing",
            "step": step,
            "logs": logs,
        }, ensure_ascii=False))
        volume.commit()
        print(f"[{elapsed}s] {message}", flush=True)

    try:
        update("downloading", "Starting download via cobalt.tools…")

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.mp3")

            # ── Step 1: get direct audio URL from cobalt.tools ──────────────
            import httpx, re
            cobalt_url = None
            try:
                r = httpx.post(
                    "https://api.cobalt.tools/",
                    json={"url": youtube_url, "downloadMode": "audio", "audioFormat": "mp3"},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=30,
                )
                cdata = r.json()
                update("downloading", f"cobalt status: {cdata.get('status')} — {str(cdata)[:120]}")
                if cdata.get("status") in ("stream", "redirect", "tunnel", "picker"):
                    cobalt_url = cdata.get("url") or (cdata.get("picker") or [{}])[0].get("url")
            except Exception as e:
                update("downloading", f"cobalt request failed: {e}")

            # ── Step 2: download audio from cobalt URL ───────────────────────
            if cobalt_url:
                update("downloading", f"Downloading from cobalt URL…")
                with httpx.stream("GET", cobalt_url, follow_redirects=True, timeout=120) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with open(audio_path, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = round(downloaded / total * 100)
                                if pct % 20 == 0:
                                    update("downloading", f"Downloading: {pct}%")
                title = "Arabic Lecture"
                # get title separately via yt-dlp (info only, no download)
                try:
                    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
                        info = ydl.extract_info(youtube_url, download=False)
                        title = info.get("title", title)
                except Exception:
                    pass
                update("downloading", f"Download complete. Title: '{title}'")
            else:
                raise RuntimeError("cobalt.tools could not provide a download URL. Try uploading the audio file directly instead.")

            size_mb = round(os.path.getsize(audio_path) / 1024 / 1024, 1)
            update("transcribing", f"Audio file: {size_mb} MB. Sending to Whisper…")

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
            update("translating", f"Whisper done. Got {len(segments)} segments, {len(words)} words. Starting translation…")

            translated_segments = []
            for i, seg in enumerate(segments):
                if i % 5 == 0:
                    update("translating", f"Translating segment {i+1}/{len(segments)}…")
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

            update("building_html", "Translation done. Building HTML file…")

            with open(audio_path, "rb") as af:
                audio_b64 = base64.b64encode(af.read()).decode("utf-8")

            html = _build_html(title, audio_b64, translated_segments, words)

            html_path = Path(STORAGE_PATH) / f"{job_id}.html"
            html_path.write_text(html, encoding="utf-8")

            total = round(time.time() - start_time, 1)
            logs.append({"t": total, "msg": f"Done! Total time: {total}s"})
            status_path.write_text(json.dumps({
                "status": "done",
                "html_filename": f"{job_id}.html",
                "logs": logs,
            }, ensure_ascii=False))
            volume.commit()

    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


def _write_cookies(raw: str, path: str):
    """Normalise a cookies.txt export into a strict Netscape file yt-dlp accepts."""
    out = ["# Netscape HTTP Cookie File"]
    for line in raw.splitlines():
        line = line.rstrip("\r")
        # Strip HttpOnly prefix added by some exporters
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue  # skip malformed lines
        domain, subdomain, path_, secure, expiry, name, value = parts
        # Netscape format requires TRUE for dot-prefixed domains
        if domain.startswith("."):
            subdomain = "TRUE"
        out.append("\t".join([domain, subdomain, path_, secure, expiry, name, value]))
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


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
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    status_path.write_text(json.dumps({"status": "processing", "step": "queued"}))
    volume.commit()
    process_lecture.spawn(job_id, youtube_url)
    return {"status": "processing", "job_id": job_id}


@app.function(
    image=image,
    timeout=600,
    volumes={STORAGE_PATH: volume},
    secrets=[
        modal.Secret.from_name("openai-secret"),
        modal.Secret.from_name("anthropic-secret"),
    ],
)
def process_uploaded_audio(job_id: str, audio_bytes: bytes, title: str):
    import time
    import traceback
    from openai import OpenAI
    import anthropic

    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    logs = []
    start_time = time.time()

    def fail(error_msg):
        logs.append({"t": round(time.time() - start_time, 1), "msg": f"FAILED: {error_msg}"})
        status_path.write_text(json.dumps({"status": "failed", "error": error_msg, "logs": logs}, ensure_ascii=False))
        volume.commit()

    def update(step, message):
        elapsed = round(time.time() - start_time, 1)
        logs.append({"t": elapsed, "msg": message})
        status_path.write_text(json.dumps({"status": "processing", "step": step, "logs": logs}, ensure_ascii=False))
        volume.commit()

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.mp3")
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)
            size_mb = round(len(audio_bytes) / 1024 / 1024, 1)
            update("transcribing", f"Received {size_mb} MB audio. Sending to Whisper…")

            with open(audio_path, "rb") as af:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1", file=af,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                    language="ar",
                )

            segments = transcript.segments
            words = transcript.words if hasattr(transcript, "words") and transcript.words else []
            update("translating", f"Whisper done. {len(segments)} segments. Translating…")

            translated_segments = []
            for i, seg in enumerate(segments):
                if i % 5 == 0:
                    update("translating", f"Translating segment {i+1}/{len(segments)}…")
                resp = anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=512,
                    messages=[{"role": "user", "content": f"Translate this Arabic text to clear English for general comprehension. Keep it natural and readable. Return only the translation, nothing else.\n\n{seg.text}"}]
                )
                translated_segments.append({"id": seg.id, "start": seg.start, "end": seg.end,
                                            "arabic": seg.text.strip(), "english": resp.content[0].text.strip()})

            update("building_html", "Translation done. Building HTML…")
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            html = _build_html(title, audio_b64, translated_segments, words)
            html_path = Path(STORAGE_PATH) / f"{job_id}.html"
            html_path.write_text(html, encoding="utf-8")

            total = round(time.time() - start_time, 1)
            logs.append({"t": total, "msg": f"Done! Total time: {total}s"})
            status_path.write_text(json.dumps({"status": "done", "html_filename": f"{job_id}.html", "logs": logs}, ensure_ascii=False))
            volume.commit()

    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


@app.function(image=image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="POST", label="upload")
async def upload_endpoint(request):
    from fastapi import Request
    form = await request.form()
    audio_file = form.get("audio")
    title = form.get("title", "Arabic Lecture")
    if not audio_file:
        return {"error": "audio file required"}
    audio_bytes = await audio_file.read()
    job_id = str(uuid.uuid4())
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    status_path.write_text(json.dumps({"status": "processing", "step": "queued"}))
    volume.commit()
    process_uploaded_audio.spawn(job_id, audio_bytes, title)
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
    logs = data.get("logs", [])
    if data.get("status") == "done":
        download_url = f"https://mahdid313--download.modal.run?job_id={job_id}"
        return {"status": "done", "download_url": download_url, "logs": logs}
    if data.get("status") == "failed":
        return {"status": "failed", "error": data.get("error", "Unknown error"), "logs": logs}

    return {"status": data.get("status", "processing"), "step": data.get("step", ""), "logs": logs}


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

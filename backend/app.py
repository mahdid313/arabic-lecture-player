import modal
import os
import uuid
import json
import base64
import tempfile
from pathlib import Path
try:
    from fastapi import UploadFile, Form, Request
except ImportError:
    UploadFile = Form = Request = None

app = modal.App("arabic-lecture-player")

# Lightweight image for API endpoints — boots in seconds
api_image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "fastapi",
    "uvicorn",
    "python-multipart",
)

# Heavy image for processing only — boots in ~2 min but only used once per job
worker_image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "openai",
    "anthropic",
    "fastapi",
    "uvicorn",
).apt_install("ffmpeg")

volume = modal.Volume.from_name("arabic-lecture-storage", create_if_missing=True)
STORAGE_PATH = "/storage"


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
  body {{ background: #0f0f0f; color: #e0e0e0; font-family: 'Segoe UI', Arial, sans-serif; min-height: 100vh; }}
  header {{ background: #1a1a1a; padding: 16px; position: sticky; top: 0; z-index: 10; box-shadow: 0 2px 8px rgba(0,0,0,0.5); }}
  h1 {{ font-size: 1rem; color: #fff; text-align: center; direction: ltr; margin-bottom: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  audio {{ width: 100%; height: 40px; accent-color: #4fa3e0; }}
  #transcript {{ padding: 12px; max-width: 800px; margin: 0 auto; }}
  .segment {{ background: #1c1c1c; border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; border-right: 3px solid transparent; transition: border-color 0.2s, background 0.2s; cursor: pointer; }}
  .segment.active {{ background: #1e2d3d; border-right-color: #4fa3e0; }}
  .arabic {{ font-size: 1.35rem; line-height: 1.7; color: #f0f0f0; text-align: right; direction: rtl; font-family: 'Amiri', 'Traditional Arabic', serif; }}
  .english {{ font-size: 0.88rem; color: #888; margin-top: 8px; text-align: left; direction: ltr; line-height: 1.5; }}
  .timestamp {{ font-size: 0.72rem; color: #555; text-align: left; direction: ltr; margin-top: 4px; }}
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

// Apply font size from parent app setting
(function() {{
  const pct = parseInt(localStorage.getItem('font_size_pct') || '125');
  document.documentElement.style.fontSize = pct + '%';
}})();

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


def _group_short_segments(segments: list, min_duration: float = 3.0) -> list:
    """
    Merge consecutive segments until the group spans at least min_duration seconds.
    Long segments that already meet the threshold are left alone.
    """
    groups = []
    i = 0
    while i < len(segments):
        group = [segments[i]]
        while (group[-1]["end"] - group[0]["start"]) < min_duration and i + 1 < len(segments):
            i += 1
            group.append(segments[i])
        groups.append(group)
        i += 1
    return groups


def _split_translation(translation: str, group: list) -> list:
    """
    Distribute a translated string back across group segments proportionally
    by the character length of each segment's Arabic text.
    """
    if len(group) == 1:
        return [translation]
    arabic_lens = [max(len(s["arabic"]), 1) for s in group]
    total = sum(arabic_lens)
    words = translation.split()
    n = len(words)
    result, idx = [], 0
    for k, ln in enumerate(arabic_lens):
        if k == len(arabic_lens) - 1:
            result.append(" ".join(words[idx:]))
        else:
            take = max(1, round(ln / total * n))
            result.append(" ".join(words[idx:idx + take]))
            idx += take
    return result


@app.function(
    image=worker_image,
    timeout=600,
    volumes={STORAGE_PATH: volume},
    secrets=[
        modal.Secret.from_name("openai-secret"),
        modal.Secret.from_name("anthropic-secret"),
    ],
)
def process_uploaded_audio(job_id: str, title: str):
    """Reads audio from Volume (saved by upload_endpoint) and processes it."""
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
        print(f"[{elapsed}s] {message}", flush=True)

    try:
        import math
        import subprocess
        import tempfile

        # Check for saved transcript checkpoint (allows resuming after Whisper succeeds)
        volume.reload()
        transcript_path = Path(STORAGE_PATH) / f"{job_id}_transcript.json"
        audio_bytes = None  # loaded lazily below if Whisper needed

        if transcript_path.exists():
            update("translating", "Resuming from saved transcript checkpoint — skipping Whisper…")
            td = json.loads(transcript_path.read_text())
            raw_segments = td["segments"]
            audio_duration_s = td.get("duration", 0)
            words_raw = td.get("words", [])
            words_list = [type("W", (), w)() for w in words_raw]
            # Load audio bytes for HTML embedding (audio file may still be present)
            candidates = list(Path(STORAGE_PATH).glob(f"{job_id}_audio.*"))
            if candidates:
                audio_bytes = Path(candidates[0]).read_bytes()
            else:
                raise RuntimeError("Audio file missing — cannot rebuild HTML after checkpoint resume")
        else:
            # Find the audio file saved by upload_endpoint
            candidates = list(Path(STORAGE_PATH).glob(f"{job_id}_audio.*"))
            if not candidates:
                raise RuntimeError("Audio file not found in storage")
            audio_path = Path(candidates[0])
            audio_bytes = audio_path.read_bytes()
            size_mb = round(len(audio_bytes) / 1024 / 1024, 1)

            # Whisper limit is 25 MB — split with ffmpeg if needed
            WHISPER_MAX = 24 * 1024 * 1024
            if len(audio_bytes) > WHISPER_MAX:
                update("transcribing", f"Received {size_mb} MB audio — splitting for Whisper (25 MB limit)…")
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)],
                    capture_output=True, text=True, check=True,
                )
                total_dur = float(json.loads(probe.stdout)["format"]["duration"])
                n_parts = math.ceil(len(audio_bytes) / WHISPER_MAX) + 1
                seg_dur = total_dur / n_parts
                ext = audio_path.suffix
                with tempfile.TemporaryDirectory() as tmp:
                    pattern = os.path.join(tmp, f"part_%03d{ext}")
                    subprocess.run([
                        "ffmpeg", "-i", str(audio_path),
                        "-f", "segment", "-segment_time", str(seg_dur),
                        "-c", "copy", "-reset_timestamps", "1", pattern,
                    ], check=True, capture_output=True)
                    part_files = sorted(Path(tmp).glob(f"part_*{ext}"))
                    update("transcribing", f"Split into {len(part_files)} parts. Transcribing…")

                    raw_segments, words_list, audio_duration_s = [], [], 0.0
                    for pi, pf in enumerate(part_files):
                        update("transcribing", f"Transcribing part {pi+1}/{len(part_files)}…")
                        with open(pf, "rb") as af:
                            tr = openai_client.audio.transcriptions.create(
                                model="whisper-1", file=af,
                                response_format="verbose_json",
                                timestamp_granularities=["word", "segment"],
                                language="ar",
                            )
                        offset = audio_duration_s
                        part_dur = getattr(tr, "duration", 0) or 0
                        audio_duration_s += part_dur
                        for seg in tr.segments:
                            raw_segments.append({"id": len(raw_segments), "start": seg.start + offset, "end": seg.end + offset, "arabic": seg.text.strip()})
                        if tr.words:
                            for w in tr.words:
                                words_list.append(type("W", (), {"start": w.start + offset, "end": w.end + offset, "word": w.word})())
            else:
                update("transcribing", f"Received {size_mb} MB audio. Sending to Whisper…")
                with open(audio_path, "rb") as af:
                    transcript = openai_client.audio.transcriptions.create(
                        model="whisper-1", file=af,
                        response_format="verbose_json",
                        timestamp_granularities=["word", "segment"],
                        language="ar",
                    )
                audio_duration_s = getattr(transcript, "duration", 0) or 0
                raw_segments = [
                    {"id": seg.id, "start": seg.start, "end": seg.end, "arabic": seg.text.strip()}
                    for seg in transcript.segments
                ]
                words_list = transcript.words if hasattr(transcript, "words") and transcript.words else []

            # Save transcript checkpoint so retries can skip Whisper
            transcript_path.write_text(json.dumps({
                "segments": raw_segments,
                "duration": audio_duration_s,
                "words": [{"start": w.start, "end": w.end, "word": w.word} for w in (words_list or [])],
            }, ensure_ascii=False))
            volume.commit()

        # Whisper cost: $0.006 / minute
        whisper_cost = round(audio_duration_s / 60 * 0.006, 4)
        words = words_list
        update("translating", f"Whisper done. {len(raw_segments)} segments ({round(audio_duration_s/60,1)} min, ${whisper_cost}). Translating…")

        # Group short segments so Claude gets enough context per call
        groups = _group_short_segments(raw_segments, min_duration=3.0)
        update("translating", f"Grouped into {len(groups)} translation batches.")

        # Claude Haiku pricing (claude-haiku-4-5): $0.80 / 1M input, $4.00 / 1M output
        HAIKU_IN  = 0.80 / 1_000_000
        HAIKU_OUT = 4.00 / 1_000_000
        total_in_tokens = 0
        total_out_tokens = 0

        translated_segments = []
        for gi, group in enumerate(groups):
            if gi % 5 == 0:
                update("translating", f"Translating batch {gi+1}/{len(groups)}…")
            combined = " ".join(s["arabic"] for s in group)
            resp = anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=1024,
                messages=[{"role": "user", "content":
                    "You are translating an Arabic Islamic lecture. Translate naturally into clear English "
                    "for general comprehension. The speaker may use Gulf Arabic dialect. Never add notes, "
                    "alternatives, or uncertainty — just give the best natural translation. "
                    "Return only the translation.\n\n" + combined
                }]
            )
            total_in_tokens  += resp.usage.input_tokens
            total_out_tokens += resp.usage.output_tokens
            translation = resp.content[0].text.strip()
            parts = _split_translation(translation, group)
            for seg, eng in zip(group, parts):
                translated_segments.append({**seg, "english": eng})

        claude_cost = round(total_in_tokens * HAIKU_IN + total_out_tokens * HAIKU_OUT, 4)
        total_cost  = round(whisper_cost + claude_cost, 4)
        costs = {
            "whisper_minutes": round(audio_duration_s / 60, 2),
            "whisper_usd":     whisper_cost,
            "claude_in_tok":   total_in_tokens,
            "claude_out_tok":  total_out_tokens,
            "claude_usd":      claude_cost,
            "total_usd":       total_cost,
        }
        update("building_html", f"Translation done. Cost so far: ${total_cost}. Building HTML…")

        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        html = _build_html(title, audio_b64, translated_segments, words)
        html_path = Path(STORAGE_PATH) / f"{job_id}.html"
        html_path.write_text(html, encoding="utf-8")

        # Clean up audio and transcript checkpoint files
        for p in Path(STORAGE_PATH).glob(f"{job_id}_audio.*"):
            p.unlink(missing_ok=True)
        transcript_path.unlink(missing_ok=True)

        import time as _t
        total_time = round(time.time() - start_time, 1)
        logs.append({"t": total_time, "msg": f"Done! Total time: {total_time}s | Cost: ${total_cost}"})
        status_path.write_text(json.dumps({
            "status": "done",
            "title": title,
            "timestamp": _t.time(),
            "html_filename": f"{job_id}.html",
            "costs": costs,
            "logs": logs,
        }, ensure_ascii=False))
        volume.commit()

    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="POST", label="process")
def process_endpoint(body: dict):
    """Accept a YouTube URL from the phone. Queues it for the local PC agent to download."""
    youtube_url = body.get("youtube_url", "").strip()
    if not youtube_url:
        return {"error": "youtube_url is required"}, 400
    job_id = str(uuid.uuid4())
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    status_path.write_text(json.dumps({
        "status": "processing",
        "step": "waiting_download",
        "youtube_url": youtube_url,
    }))
    volume.commit()
    return {"status": "processing", "job_id": job_id}


@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(label="agent")
async def agent_endpoint(request):
    """
    Dual-purpose PC-agent endpoint.
    GET  → returns pending download jobs (replaces /queue)
    POST → accepts progress/failure reports from the downloader (replaces /report)
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    import time as _time

    if request.method == "GET":
        volume.reload()
        storage = Path(STORAGE_PATH)
        pending = []
        for f in storage.glob("*_status.json"):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            if data.get("step") == "waiting_download" and data.get("status") == "processing":
                job_id = f.name.replace("_status.json", "")
                youtube_url = data.get("youtube_url", "")
                if not youtube_url:
                    continue
                data["step"] = "downloading"
                f.write_text(json.dumps(data))
                pending.append({"job_id": job_id, "youtube_url": youtube_url})
        if pending:
            volume.commit()
        return JSONResponse({"jobs": pending})

    # POST — progress report from PC downloader
    body = await request.json()
    job_id = body.get("job_id", "").strip()
    status = body.get("status", "processing")
    step   = body.get("step", "downloading")
    message = body.get("message", "")
    if not job_id:
        return {"error": "job_id required"}
    volume.reload()
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    if not status_path.exists():
        return {"error": "job not found"}
    try:
        data = json.loads(status_path.read_text())
    except Exception:
        data = {}
    logs = data.get("logs", [])
    logs.append({"t": round(_time.time() % 100000, 1), "msg": message})
    if status == "failed":
        data = {"status": "failed", "error": message, "logs": logs,
                "youtube_url": data.get("youtube_url", "")}
    else:
        data = {"status": "processing", "step": step, "logs": logs,
                "youtube_url": data.get("youtube_url", "")}
    status_path.write_text(json.dumps(data, ensure_ascii=False))
    volume.commit()
    return JSONResponse({"ok": True})


@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="POST", label="upload")
async def upload_endpoint(
    audio: UploadFile,
    title: str = Form("Arabic Lecture"),
    job_id: str = Form(""),
    chunk_index: int = Form(-1),
    total_chunks: int = Form(1),
):
    """
    Accepts a multipart audio upload — either a single file or one chunk of a
    chunked upload. Chunked uploads are reassembled once all chunks arrive.
    """
    if not audio:
        return {"error": "audio file required"}

    job_id = job_id.strip() or str(uuid.uuid4())
    ext = Path(audio.filename or "audio.m4a").suffix.lower() or ".m4a"
    chunk_data = await audio.read()

    volume.reload()

    # ── Chunked upload ────────────────────────────────────────────────────────
    if chunk_index >= 0 and total_chunks > 1:
        chunk_path = Path(STORAGE_PATH) / f"{job_id}_chunk_{chunk_index}{ext}"
        chunk_path.write_bytes(chunk_data)

        # Write/update status so polling shows something
        status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
        received = len(list(Path(STORAGE_PATH).glob(f"{job_id}_chunk_*")))
        status_path.write_text(json.dumps({
            "status": "processing",
            "step": "queued",
            "logs": [{"t": 0, "msg": f"Uploading… chunk {received}/{total_chunks}"}],
        }))
        volume.commit()

        if received < total_chunks:
            return {"status": "uploading", "job_id": job_id, "received": received}

        # All chunks received — concatenate in order
        audio_path = Path(STORAGE_PATH) / f"{job_id}_audio{ext}"
        with open(audio_path, "wb") as out:
            for i in range(total_chunks):
                p = Path(STORAGE_PATH) / f"{job_id}_chunk_{i}{ext}"
                out.write(p.read_bytes())
                p.unlink()
    else:
        # ── Single upload ─────────────────────────────────────────────────────
        audio_path = Path(STORAGE_PATH) / f"{job_id}_audio{ext}"
        audio_path.write_bytes(chunk_data)

    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    status_path.write_text(json.dumps({"status": "processing", "step": "queued"}))
    volume.commit()

    process_uploaded_audio.spawn(job_id, title)
    return {"status": "processing", "job_id": job_id}



@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="GET", label="status")
def status_endpoint(job_id: str):
    volume.reload()
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    if not status_path.exists():
        return {"status": "not_found"}

    data = json.loads(status_path.read_text())
    logs = data.get("logs", [])

    if data.get("status") == "done":
        return {"status": "done", "title": data.get("title", ""), "download_url": f"https://mahdid313--download.modal.run?job_id={job_id}", "logs": logs}
    if data.get("status") == "failed":
        return {"status": "failed", "error": data.get("error", "Unknown error"), "logs": logs}

    return {"status": data.get("status", "processing"), "step": data.get("step", ""), "logs": logs}


@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="POST", label="rename")
def rename_endpoint(body: dict):
    job_id = body.get("job_id", "").strip()
    title  = body.get("title", "").strip()
    if not job_id or not title:
        return {"error": "job_id and title required"}
    volume.reload()
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    if not status_path.exists():
        return {"error": "job not found"}
    try:
        data = json.loads(status_path.read_text())
    except Exception:
        return {"error": "could not read job"}
    data["title"] = title
    status_path.write_text(json.dumps(data, ensure_ascii=False))
    volume.commit()
    return {"ok": True}


@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="POST", label="retry")
def retry_endpoint(body: dict):
    """Re-spawn processing for a failed job. If a Whisper transcript checkpoint exists it will be reused."""
    job_id = body.get("job_id", "").strip()
    if not job_id:
        return {"error": "job_id required"}
    volume.reload()
    status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
    if not status_path.exists():
        return {"error": "job not found"}
    # Must have either audio or transcript checkpoint to retry
    has_audio = bool(list(Path(STORAGE_PATH).glob(f"{job_id}_audio.*")))
    has_checkpoint = (Path(STORAGE_PATH) / f"{job_id}_transcript.json").exists()
    if not has_audio and not has_checkpoint:
        return {"error": "Audio and transcript both missing — cannot retry. Please re-upload the file."}
    try:
        data = json.loads(status_path.read_text())
    except Exception:
        data = {}
    title = data.get("title", "Arabic Lecture")
    has_transcript = has_checkpoint
    status_path.write_text(json.dumps({
        "status": "processing",
        "step": "translating" if has_transcript else "queued",
        "title": title,
        "logs": [{"t": 0, "msg": "Retrying" + (" from transcript checkpoint" if has_transcript else "") + "…"}],
    }, ensure_ascii=False))
    volume.commit()
    process_uploaded_audio.spawn(job_id, title)
    return {"ok": True, "job_id": job_id, "from_checkpoint": has_transcript}


@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="GET", label="download")
def download_endpoint(job_id: str, dl: str = "0"):
    from fastapi.responses import HTMLResponse
    volume.reload()
    html_path = Path(STORAGE_PATH) / f"{job_id}.html"
    if not html_path.exists():
        return {"error": "file not found"}
    content = html_path.read_text(encoding="utf-8")
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length",
    }
    if dl == "1":
        headers["Content-Disposition"] = f'attachment; filename="lecture-{job_id[:8]}.html"'
    return HTMLResponse(content=content, headers=headers)


@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.fastapi_endpoint(method="GET", label="library")
def library_endpoint():
    from fastapi.responses import JSONResponse
    import re as _re
    import time as _time
    from datetime import datetime, timezone

    volume.reload()
    lectures = []

    def empty_totals():
        return {"whisper_usd": 0, "claude_usd": 0, "total_usd": 0, "count": 0}

    totals = empty_totals()
    month_totals = empty_totals()
    year_totals  = empty_totals()

    now = datetime.now(timezone.utc)
    cur_year  = now.year
    cur_month = now.month

    for f in Path(STORAGE_PATH).glob("*_status.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if data.get("status") == "done":
            job_id = f.name.replace("_status.json", "")
            title = data.get("title", "")
            if not title:
                html_path = Path(STORAGE_PATH) / f"{job_id}.html"
                if html_path.exists():
                    head = html_path.read_text(encoding="utf-8", errors="ignore")[:400]
                    m = _re.search(r"<title>(.*?)</title>", head)
                    title = m.group(1) if m else job_id[:8]
                else:
                    title = job_id[:8]
            costs = data.get("costs", {})
            ts = data.get("timestamp", 0)
            lectures.append({"job_id": job_id, "title": title, "timestamp": ts, "costs": costs})

            def _add(bucket, c):
                bucket["whisper_usd"] += c.get("whisper_usd", 0)
                bucket["claude_usd"]  += c.get("claude_usd", 0)
                bucket["total_usd"]   += c.get("total_usd", 0)
                bucket["count"]       += 1

            _add(totals, costs)
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                if dt.year == cur_year:
                    _add(year_totals, costs)
                    if dt.month == cur_month:
                        _add(month_totals, costs)

    for bucket in (totals, month_totals, year_totals):
        for k in ("whisper_usd", "claude_usd", "total_usd"):
            bucket[k] = round(bucket[k], 4)

    lectures.sort(key=lambda x: x["timestamp"], reverse=True)
    return JSONResponse(
        {"lectures": lectures, "totals": totals,
         "month_totals": month_totals, "year_totals": year_totals},
        headers={"Access-Control-Allow-Origin": "*"},
    )

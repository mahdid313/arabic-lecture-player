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


def _build_html(title: str, audio_b64: str, segments: list, words: list, audio_mime: str = "audio/mp4") -> str:
    import html as _html

    # Pre-render every segment as static HTML so text appears as bytes stream in —
    # no JS execution needed for display.
    segs_html = []
    for i, s in enumerate(segments):
        ar  = _html.escape(s.get("arabic", ""))
        en  = _html.escape(s.get("english", ""))
        t0, t1 = s["start"], s["end"]
        ts  = f"{int(t0)//60}:{int(t0)%60:02d} – {int(t1)//60}:{int(t1)%60:02d}"
        segs_html.append(
            f'<div class="segment" id="seg-{i}" data-s="{t0:.3f}" data-e="{t1:.3f}">'
            f'<div class="arabic">{ar}</div>'
            f'<div class="english">{en}</div>'
            f'<div class="timestamp">{ts}</div>'
            f'</div>'
        )

    title_esc   = _html.escape(title)
    transcript  = "\n".join(segs_html)

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_esc}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #0f0f0f; color: #e0e0e0; font-family: 'Segoe UI', Arial, sans-serif; min-height: 100vh; }}
header {{ background: #1a1a1a; padding: 16px; position: sticky; top: 0; z-index: 10; box-shadow: 0 2px 8px rgba(0,0,0,.5); }}
h1 {{ font-size: 1rem; color: #fff; text-align: center; direction: ltr; margin-bottom: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
audio {{ width: 100%; height: 40px; accent-color: #4fa3e0; }}
#audio-bar {{ font-size: 0.75rem; color: #4fa3e0; text-align: center; padding: 4px 0 0; }}
#transcript {{ padding: 12px; max-width: 800px; margin: 0 auto; }}
.segment {{ background: #1c1c1c; border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; border-right: 3px solid transparent; transition: border-color .2s, background .2s; cursor: pointer; }}
.segment.active {{ background: #1e2d3d; border-right-color: #4fa3e0; }}
.arabic {{ font-size: 1.35rem; line-height: 1.7; color: #f0f0f0; text-align: right; direction: rtl; font-family: 'Amiri','Traditional Arabic',serif; }}
.english {{ font-size: 0.88rem; color: #888; margin-top: 8px; text-align: left; direction: ltr; line-height: 1.5; }}
.timestamp {{ font-size: 0.72rem; color: #555; text-align: left; direction: ltr; margin-top: 4px; }}
</style>
</head>
<body>
<header>
  <h1>{title_esc}</h1>
  <audio id="player" controls preload="none"></audio>
  <div id="audio-bar">⏳ Audio loading…</div>
</header>
<div id="transcript">
{transcript}
</div>
<!-- Tiny interaction script — runs right after static text is visible -->
<script>
(function(){{
  var pct = parseInt(localStorage.getItem('font_size_pct')||'125');
  document.documentElement.style.fontSize = pct + '%';
  var player = document.getElementById('player');
  var segs   = document.querySelectorAll('.segment');
  segs.forEach(function(el){{
    el.addEventListener('click', function(){{
      player.currentTime = parseFloat(el.dataset.s); player.play();
    }});
  }});
  var last = -1;
  player.addEventListener('timeupdate', function(){{
    var t = player.currentTime, active = -1;
    for(var i=0;i<segs.length;i++){{
      if(t>=parseFloat(segs[i].dataset.s)&&t<parseFloat(segs[i].dataset.e)){{active=i;break;}}
    }}
    if(active===last)return;
    if(last>=0)segs[last].classList.remove('active');
    if(active>=0){{segs[active].classList.add('active');segs[active].scrollIntoView({{behavior:'smooth',block:'center'}});}}
    last=active;
  }});
  // Notify parent: transcript is rendered, hide the loading overlay
  window.parent.postMessage({{type:'lectureReady'}},'*');
}})();
</script>
<!-- Audio base64 last — 20-30 MB, loads after text is already visible -->
<script>
(function(){{
  var p=document.getElementById('player');
  var bar=document.getElementById('audio-bar');
  p.src='data:{audio_mime};base64,{audio_b64}';
  p.addEventListener('canplay',function(){{if(bar)bar.style.display='none';}},{{once:true}});
}})();
</script>
</body>
</html>"""


def _merge_short_segments(segments: list, min_dur: float = 2.0) -> list:
    """Merge segments shorter than min_dur seconds into the following segment."""
    if not segments:
        return segments
    result = [dict(s) for s in segments]
    i = 0
    while i < len(result) - 1:
        if (result[i]["end"] - result[i]["start"]) < min_dur:
            result[i]["end"] = result[i + 1]["end"]
            result[i]["arabic"] = (result[i]["arabic"] + " " + result[i + 1]["arabic"]).strip()
            result.pop(i + 1)
        else:
            i += 1
    return result


def _group_by_words(segments: list, target_words: int = 120) -> list:
    """Group segments into chunks of ~target_words Arabic words."""
    groups, current, current_wc = [], [], 0
    for seg in segments:
        wc = len(seg["arabic"].split())
        if current and current_wc + wc > int(target_words * 1.4):
            groups.append(current)
            current, current_wc = [seg], wc
        else:
            current.append(seg)
            current_wc += wc
    if current:
        groups.append(current)
    return groups


def _split_translation(translation: str, group: list) -> list:
    """
    Distribute translated text back across segments.
    Splits into sentences first, then assigns by audio duration proportion.
    """
    import re as _re
    if len(group) == 1:
        return [translation]

    # Split into sentences; fall back to words if no sentence boundaries found
    sentences = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', translation.strip()) if s.strip()]
    if not sentences:
        sentences = [translation]

    durations = [max(s["end"] - s["start"], 0.1) for s in group]
    total_dur = sum(durations)
    n_units = len(sentences)

    result = []
    pool = list(sentences)
    for k, dur in enumerate(durations):
        if k == len(durations) - 1 or not pool:
            result.append(" ".join(pool))
            pool = []
        else:
            remaining_segs = len(durations) - k
            fraction = dur / total_dur
            take = max(1, round(fraction * n_units))
            # Always leave at least one unit per remaining segment
            take = min(take, len(pool) - (remaining_segs - 1))
            take = max(take, 1)
            result.append(" ".join(pool[:take]))
            pool = pool[take:]

    # Pad with empty strings if pool somehow ran dry early
    while len(result) < len(group):
        result.append("")
    return result


@app.function(
    image=worker_image,
    timeout=3600,
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

    cancel_path = Path(STORAGE_PATH) / f"{job_id}_cancel"

    def _already_done():
        try:
            return json.loads(status_path.read_text()).get("status") == "done"
        except Exception:
            return False

    def _cancelled():
        return cancel_path.exists()

    def fail(error_msg):
        if _already_done():
            return
        logs.append({"t": round(time.time() - start_time, 1), "msg": f"FAILED: {error_msg}"})
        status_path.write_text(json.dumps({"status": "failed", "error": error_msg, "logs": logs}, ensure_ascii=False))
        volume.commit()

    def update(step, message):
        if _already_done():
            return
        if _cancelled():
            raise RuntimeError("Job cancelled — a newer retry is running")
        elapsed = round(time.time() - start_time, 1)
        logs.append({"t": elapsed, "msg": message})
        status_path.write_text(json.dumps({"status": "processing", "step": step, "logs": logs}, ensure_ascii=False))
        volume.commit()
        print(f"[{elapsed}s] {message}", flush=True)

    try:
        import math
        import subprocess
        import tempfile

        # Clear any cancel flag written by a previous retry request
        cancel_path.unlink(missing_ok=True)
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

        # Pre-merge sub-2s fragments, then group into ~350-word chunks
        clean_segments = _merge_short_segments(raw_segments, min_dur=2.0)
        groups = _group_by_words(clean_segments, target_words=350)
        update("translating", f"Grouped into {len(groups)} translation batches ({len(clean_segments)} segments after merging shorts).")

        # Claude Haiku pricing
        HAIKU_IN  = 0.80 / 1_000_000
        HAIKU_OUT = 4.00 / 1_000_000
        total_in_tokens = 0
        total_out_tokens = 0

        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading, time as _t
        _token_lock = threading.Lock()

        SYSTEM_PROMPT = (
            "You are translating a formal Arabic Islamic jurisprudence lecture into English. "
            "The speaker is a scholar using classical Islamic terminology mixed with spoken Arabic. "
            "Rules: translate naturally and fluently as if it were originally spoken in English; "
            "preserve technical Islamic terms in brackets where needed e.g. [illah], [usul]; "
            "never add notes, alternatives, or uncertainty; "
            "never leave fragments — always complete the sentence naturally using surrounding context; "
            "return only the translation with no commentary."
        )

        # Load partial translation checkpoint if it exists
        xlat_checkpoint_path = Path(STORAGE_PATH) / f"{job_id}_xlat.json"
        if xlat_checkpoint_path.exists():
            xc = json.loads(xlat_checkpoint_path.read_text())
            results = xc.get("results", [None] * len(groups))
            # Invalidate checkpoint if group count changed (e.g. different grouping strategy)
            if len(results) != len(groups):
                results = [None] * len(groups)
                xc = {}
            total_in_tokens  = xc.get("in_tok", 0)
            total_out_tokens = xc.get("out_tok", 0)
            already_done = sum(1 for r in results if r is not None)
            update("translating", f"Resuming translation from batch {already_done}/{len(groups)}…")
        else:
            results = [None] * len(groups)
            already_done = 0

        # Safe parallelism: 3 workers stays well under 50 RPM
        def _translate_one(gi_group):
            gi, group = gi_group
            if results[gi] is not None:
                return gi, group, None  # already done in previous run

            current_text = " ".join(s["arabic"] for s in group)

            # Surrounding context (~60 words each side) so Claude never sees orphaned fragments
            prev_ctx = ""
            if gi > 0:
                prev_words = " ".join(s["arabic"] for s in groups[gi - 1])
                prev_ctx = " ".join(prev_words.split()[-60:])
            next_ctx = ""
            if gi < len(groups) - 1:
                next_words = " ".join(s["arabic"] for s in groups[gi + 1])
                next_ctx = " ".join(next_words.split()[:60])

            msg_parts = []
            if prev_ctx:
                msg_parts.append(f"[Previous context — do not translate]:\n{prev_ctx}")
            msg_parts.append(f"[Translate this section]:\n{current_text}")
            if next_ctx:
                msg_parts.append(f"[Following context — do not translate]:\n{next_ctx}")
            prompt = "\n\n".join(msg_parts)

            delay = 5
            for attempt in range(6):
                try:
                    resp = anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001", max_tokens=2048,
                        system=SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    return gi, group, resp
                except Exception as e:
                    if "429" in str(e) and attempt < 5:
                        _t.sleep(delay)
                        delay = min(delay * 2, 60)
                    else:
                        raise

        completed = already_done
        CHECKPOINT_EVERY = 30
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_translate_one, (gi, group)): gi for gi, group in enumerate(groups)}
            for future in as_completed(futures):
                gi, group, resp = future.result()
                if resp is not None:
                    translation = resp.content[0].text.strip()
                    parts = _split_translation(translation, group)
                    with _token_lock:
                        total_in_tokens  += resp.usage.input_tokens
                        total_out_tokens += resp.usage.output_tokens
                        completed += 1
                        results[gi] = parts
                        if completed % CHECKPOINT_EVERY == 0:
                            xlat_checkpoint_path.write_text(json.dumps({
                                "results": results, "in_tok": total_in_tokens,
                                "out_tok": total_out_tokens,
                            }, ensure_ascii=False))
                            volume.commit()
                        if completed % 10 == 0 or completed == len(groups):
                            update("translating", f"Translating… {completed}/{len(groups)} batches done")

        translated_segments = []
        for gi, group in enumerate(groups):
            parts = results[gi] or [""] * len(group)
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
        # Persist costs now — before building HTML — so they survive any subsequent crash
        try:
            status_path.write_text(json.dumps({
                "status": "processing", "step": "building_html",
                "costs": costs, "logs": logs,
            }, ensure_ascii=False))
            volume.commit()
        except Exception:
            pass
        update("building_html", f"Translation done. Cost: ${total_cost}. Generating title…")

        # Auto-generate a meaningful title from the first few translated segments
        if not title or title in ("Arabic Lecture", "audio"):
            try:
                sample = " ".join(s["english"] for s in translated_segments[:10] if s.get("english"))[:800]
                title_resp = anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=60,
                    messages=[{"role": "user", "content":
                        "Based on this excerpt from an Arabic Islamic lecture transcript, write a concise English title "
                        "(5-8 words max). Return only the title, no quotes or punctuation at the end.\n\n" + sample
                    }]
                )
                generated = title_resp.content[0].text.strip().strip('"').strip("'")
                if generated:
                    title = generated
            except Exception:
                pass

        update("building_html", f"Building HTML…")
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        # Determine MIME from the audio file extension
        audio_candidates = list(Path(STORAGE_PATH).glob(f"{job_id}_audio.*"))
        _ext = audio_candidates[0].suffix.lower() if audio_candidates else ".m4a"
        _mime_map = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".webm": "audio/webm",
                     ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav"}
        audio_mime = _mime_map.get(_ext, "audio/mp4")
        html = _build_html(title, audio_b64, translated_segments, words, audio_mime)
        html_path = Path(STORAGE_PATH) / f"{job_id}.html"
        html_path.write_text(html, encoding="utf-8")

        # Clean up all job working files
        for p in Path(STORAGE_PATH).glob(f"{job_id}_audio.*"):
            p.unlink(missing_ok=True)
        transcript_path.unlink(missing_ok=True)
        xlat_checkpoint_path.unlink(missing_ok=True)

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


def _make_agent_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    import time as _time

    _app = FastAPI()

    @_app.get("/")
    def agent_get():
        """Return pending download jobs and claim them."""
        volume.reload()
        pending = []
        for f in Path(STORAGE_PATH).glob("*_status.json"):
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

    @_app.post("/")
    async def agent_post(request: Request):
        """Accept progress/failure reports from the PC downloader."""
        body = await request.json()
        job_id = body.get("job_id", "").strip()
        status = body.get("status", "processing")
        step   = body.get("step", "downloading")
        message = body.get("message", "")
        if not job_id:
            return JSONResponse({"error": "job_id required"})
        volume.reload()
        status_path = Path(STORAGE_PATH) / f"{job_id}_status.json"
        if not status_path.exists():
            return JSONResponse({"error": "job not found"})
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

    return _app


@app.function(image=api_image, volumes={STORAGE_PATH: volume})
@modal.asgi_app(label="agent")
def agent_endpoint():
    return _make_agent_app()


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

    return {
        "status": data.get("status", "processing"),
        "step": data.get("step", ""),
        "title": data.get("title", ""),
        "youtube_url": data.get("youtube_url", ""),
        "logs": logs,
    }


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

    # Signal any currently-running job to stop before spawning the replacement
    cancel_path = Path(STORAGE_PATH) / f"{job_id}_cancel"
    cancel_path.write_text("1")
    volume.commit()

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

    # Collect all job_ids that have a completed HTML file
    html_jobs = {f.name.replace(".html", ""): f for f in Path(STORAGE_PATH).glob("*.html")
                 if not f.name.startswith("test")}

    # Merge: start with status=done entries, then add any html-only jobs
    seen = set()
    candidate_ids = []
    for f in Path(STORAGE_PATH).glob("*_status.json"):
        job_id = f.name.replace("_status.json", "")
        try:
            data = json.loads(f.read_text())
        except Exception:
            data = {}
        if data.get("status") == "done" or job_id in html_jobs:
            candidate_ids.append((job_id, data))
            seen.add(job_id)
    # Add html-only jobs that had no status.json
    for job_id in html_jobs:
        if job_id not in seen:
            candidate_ids.append((job_id, {}))

    for job_id, data in candidate_ids:
        html_path = Path(STORAGE_PATH) / f"{job_id}.html"
        if not html_path.exists():
            continue  # no HTML = not actually done
        title = data.get("title", "")
        if not title:
            head = html_path.read_text(encoding="utf-8", errors="ignore")[:400]
            m = _re.search(r"<title>(.*?)</title>", head)
            title = m.group(1) if m else job_id[:8]
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

#!/usr/bin/env python3
"""
Arabic Lecture Player — Local PC Downloader Agent

Usage:
  pip install requests yt-dlp
  python downloader.py
"""

import sys
import os
import time
import tempfile
import subprocess
import requests
from pathlib import Path

AGENT_URL  = "https://mahdid313--agent.modal.run"
UPLOAD_URL = "https://mahdid313--upload.modal.run"
POLL_INTERVAL = 5


def report(job_id, status, step, message):
    try:
        requests.post(AGENT_URL, json={
            "job_id": job_id, "status": status, "step": step, "message": message,
        }, timeout=10)
    except Exception:
        pass


def get_title(youtube_url):
    result = subprocess.run(
        ["yt-dlp", "--print", "title", "--no-playlist", youtube_url],
        capture_output=True, text=True,
    )
    return result.stdout.strip() or "Arabic Lecture"


def download_audio(youtube_url, out_template):
    """Download best native audio format — no ffmpeg/conversion needed."""
    result = subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "--no-playlist",
            "--no-embed-metadata",    # skip ffprobe metadata step
            "--no-embed-thumbnail",   # skip ffmpeg thumbnail step
            "--no-post-overwrites",
            "-o", out_template,
            youtube_url,
        ],
        capture_output=True,
        text=True,
    )
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    combined = (stdout + "\n" + stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(combined or "yt-dlp exited with error")
    return combined  # return output for logging


def upload_audio(job_id, audio_path, title):
    ext = Path(audio_path).suffix.lower()
    mime = {".m4a": "audio/mp4", ".webm": "audio/webm", ".ogg": "audio/ogg",
            ".mp3": "audio/mpeg", ".wav": "audio/wav"}.get(ext, "audio/mpeg")
    with open(audio_path, "rb") as f:
        resp = requests.post(
            UPLOAD_URL,
            files={"audio": (Path(audio_path).name, f, mime)},
            data={"title": title, "job_id": job_id},
            timeout=600,
        )
    resp.raise_for_status()
    return resp.json()


def poll_once():
    try:
        resp = requests.get(AGENT_URL, timeout=15)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
    except Exception as e:
        print(f"[queue] poll error: {e}", flush=True)
        return

    for job in jobs:
        job_id = job["job_id"]
        youtube_url = job["youtube_url"]
        short = job_id[:8]
        print(f"\n[{short}] Queued: {youtube_url}", flush=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_template = os.path.join(tmpdir, "audio.%(ext)s")
            try:
                report(job_id, "processing", "downloading", "Fetching title…")
                title = get_title(youtube_url)
                print(f"[{short}] Title: {title}", flush=True)
                report(job_id, "processing", "downloading", f"Downloading: {title}")

                ydl_output = download_audio(youtube_url, out_template)
                print(f"[{short}] yt-dlp done.", flush=True)

                candidates = list(Path(tmpdir).glob("audio.*"))
                if not candidates:
                    raise RuntimeError("yt-dlp finished but no output file found")
                actual_path = str(candidates[0])
                size_mb = round(os.path.getsize(actual_path) / 1024 / 1024, 1)
                print(f"[{short}] Downloaded {size_mb} MB ({Path(actual_path).suffix}). Uploading…", flush=True)
                report(job_id, "processing", "downloading", f"Download done ({size_mb} MB). Uploading to cloud…")

                result = upload_audio(job_id, actual_path, title)
                print(f"[{short}] Upload done: {result}", flush=True)

            except Exception as e:
                msg = str(e)
                print(f"[{short}] FAILED: {msg}", flush=True)
                report(job_id, "failed", "downloading", f"PC download failed: {msg}")


def main():
    print("Arabic Lecture Player — PC Downloader Agent")
    print(f"Polling every {POLL_INTERVAL}s. Press Ctrl+C to stop.\n", flush=True)

    r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: yt-dlp not found. Run: pip install yt-dlp", file=sys.stderr)
        sys.exit(1)
    print(f"yt-dlp {r.stdout.strip()} ready.\n", flush=True)

    while True:
        poll_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

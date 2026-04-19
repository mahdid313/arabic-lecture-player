/* global CONFIG */

const urlInput = document.getElementById("url-input");
const processBtn = document.getElementById("process-btn");
const statusCard = document.getElementById("status-card");
const statusText = document.getElementById("status-text");
const statusStep = document.getElementById("status-step");
const spinnerEl = document.getElementById("spinner");
const doneIcon = document.getElementById("done-icon");
const downloadBtn = document.getElementById("download-btn");
const logPanel = document.getElementById("log-panel");
const logToggle = document.getElementById("log-toggle");
const logOutput = document.getElementById("log-output");
const historyCard = document.getElementById("history-card");
const historyList = document.getElementById("history-list");
const noHistory = document.getElementById("no-history");

let pollTimer = null;
let currentJobId = null;
let notFoundRetries = 0;
const NOT_FOUND_MAX_RETRIES = 6;

// ── Log panel ─────────────────────────────────────────────────────────────────

let logOpen = false;
logToggle.addEventListener("click", () => {
  logOpen = !logOpen;
  logOutput.style.display = logOpen ? "block" : "none";
  logToggle.textContent = (logOpen ? "▼" : "▶") + " Show live log";
  if (logOpen) logOutput.scrollTop = logOutput.scrollHeight;
});

function renderLogs(logs) {
  if (!logs || logs.length === 0) return;
  logPanel.style.display = "block";
  logOutput.textContent = logs.map((e) => `[${String(e.t).padStart(6)}s] ${e.msg}`).join("\n");
  if (logOpen) logOutput.scrollTop = logOutput.scrollHeight;
}

const STEP_LABELS = {
  queued: "Queued, waiting for worker…",
  downloading: "Downloading audio…",
  transcribing: "Transcribing with Whisper…",
  translating: "Translating with Claude…",
  building_html: "Building HTML file…",
};

// ── History ──────────────────────────────────────────────────────────────────

function loadHistory() {
  return JSON.parse(localStorage.getItem("job_history") || "[]");
}

function updateHistoryJob(jobId, patch) {
  const history = loadHistory();
  const idx = history.findIndex((j) => j.jobId === jobId);
  if (idx >= 0) Object.assign(history[idx], patch);
  localStorage.setItem("job_history", JSON.stringify(history));
  renderHistory();
}

function saveJob(job) {
  const history = loadHistory();
  history.unshift(job);
  localStorage.setItem("job_history", JSON.stringify(history.slice(0, 20)));
  renderHistory();
}

function renderHistory() {
  const history = loadHistory();
  if (history.length === 0) {
    noHistory.style.display = "block";
    historyCard.style.display = "none";
    return;
  }
  noHistory.style.display = "none";
  historyCard.style.display = "block";
  historyList.innerHTML = "";

  history.forEach((job) => {
    const item = document.createElement("div");
    item.className = "history-item";
    const title = job.title || job.jobId.slice(0, 8);
    const time = new Date(job.timestamp).toLocaleDateString();

    let badge;
    if (job.status === "done" && job.downloadUrl) {
      badge = `<a href="${escHtml(job.downloadUrl)}" download class="badge badge-done">↓ Download</a>`;
    } else if (job.status === "failed") {
      badge = `<button class="badge badge-failed">✕ Failed</button>`;
    } else {
      badge = `<button class="badge badge-pending">⟳ Check</button>`;
    }

    item.innerHTML = `
      <div class="history-main">
        <span class="history-title" title="${escHtml(title)}">${escHtml(title)}</span>
        <span class="history-time">${time}</span>
      </div>
      <div class="history-actions">${badge}</div>
    `;

    // Clicking "Check" resumes polling; clicking "Failed" shows its logs
    if (job.status !== "done") {
      item.querySelector("button").addEventListener("click", () => checkJob(job));
    }

    historyList.appendChild(item);
  });
}

function checkJob(job) {
  clearInterval(pollTimer);
  notFoundRetries = 0;
  currentJobId = job.jobId;
  processBtn.disabled = true;

  if (job.status === "failed") {
    showError(job.error || "Unknown error");
    renderLogs(job.logs || []);
    processBtn.disabled = false;
    return;
  }

  showStatus("Checking status…", "", true);
  startPolling(job.jobId, job.title);
}

function escHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Process ───────────────────────────────────────────────────────────────────

// ── Upload ────────────────────────────────────────────────────────────────────

document.getElementById("upload-btn").addEventListener("click", async () => {
  const fileInput = document.getElementById("audio-file");
  const progressEl = document.getElementById("upload-progress");
  const file = fileInput.files[0];
  if (!file) { fileInput.click(); return; }

  document.getElementById("upload-btn").disabled = true;
  progressEl.style.display = "block";
  progressEl.textContent = `Uploading ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)…`;
  showStatus("Uploading audio…", "This may take a moment for large files.", true);

  try {
    const form = new FormData();
    form.append("audio", file);
    form.append("title", file.name.replace(/\.[^.]+$/, ""));
    const res = await fetch(CONFIG.UPLOAD_URL, { method: "POST", body: form });
    if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
    const data = await res.json();
    currentJobId = data.job_id;
    saveJob({ jobId: currentJobId, title: file.name, timestamp: Date.now(), status: "pending" });
    progressEl.style.display = "none";
    startPolling(currentJobId, file.name, true);
  } catch (err) {
    showError(err.message);
    progressEl.style.display = "none";
  }
  document.getElementById("upload-btn").disabled = false;
});

// ── Process ───────────────────────────────────────────────────────────────────

processBtn.addEventListener("click", async () => {
  const youtubeUrl = urlInput.value.trim();
  if (!youtubeUrl) { urlInput.focus(); return; }

  processBtn.disabled = true;
  showStatus("Submitting…", "", true);

  try {
    const res = await fetch(CONFIG.PROCESS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ youtube_url: youtubeUrl }),
    });
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const data = await res.json();
    currentJobId = data.job_id;

    saveJob({ jobId: currentJobId, title: youtubeUrl, timestamp: Date.now(), status: "pending" });
    startPolling(currentJobId, youtubeUrl, true);
  } catch (err) {
    showError(err.message);
    processBtn.disabled = false;
  }
});

function startPolling(jobId, originalUrl, isNewJob = false) {
  clearInterval(pollTimer);
  notFoundRetries = 0;
  pollTimer = setInterval(() => pollStatus(jobId, originalUrl, isNewJob), 5000);
  pollStatus(jobId, originalUrl, isNewJob);
}

async function pollStatus(jobId, originalUrl, isNewJob = false) {
  try {
    const res = await fetch(`${CONFIG.STATUS_URL}?job_id=${encodeURIComponent(jobId)}`);
    if (!res.ok) return;
    const data = await res.json();

    if (data.status === "done") {
      clearInterval(pollTimer);
      updateHistoryJob(jobId, { status: "done", downloadUrl: data.download_url });
      showDone(data.download_url);
      processBtn.disabled = false;

    } else if (data.status === "failed") {
      clearInterval(pollTimer);
      const errShort = (data.error || "").split("\n")[0];
      updateHistoryJob(jobId, { status: "failed", error: errShort, logs: data.logs });
      showError(errShort);
      processBtn.disabled = false;

    } else if (data.status === "not_found") {
      notFoundRetries++;
      const giveUp = !isNewJob || notFoundRetries >= NOT_FOUND_MAX_RETRIES;
      if (giveUp) {
        clearInterval(pollTimer);
        updateHistoryJob(jobId, { status: "failed", error: "Job not found on server (may have expired)" });
        showError("Job not found on server — it may have expired or failed before logging started.");
        processBtn.disabled = false;
      } else {
        showStatus("Processing lecture…", "Starting up…", true);
      }

    } else {
      const stepLabel = STEP_LABELS[data.step] || "Processing…";
      showStatus("Processing lecture…", stepLabel, true);
    }

    // render logs after show* so showStatus() doesn't wipe them
    renderLogs(data.logs);
  } catch (_) {
    // transient network error, keep polling
  }
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function showStatus(main, sub, loading) {
  statusCard.style.display = "block";
  statusText.textContent = main;
  statusStep.textContent = sub;
  spinnerEl.style.display = loading ? "block" : "none";
  doneIcon.style.display = "none";
  downloadBtn.style.display = "none";
  logPanel.style.display = "none";
  logOutput.textContent = "";
}

function showDone(downloadUrl) {
  statusCard.style.display = "block";
  statusText.textContent = "Your lecture is ready!";
  statusStep.textContent = "Tap the button below to download the HTML file.";
  spinnerEl.style.display = "none";
  doneIcon.style.display = "block";
  downloadBtn.style.display = "block";
  downloadBtn.onclick = () => { window.location.href = downloadUrl; };
}

function showError(msg) {
  statusCard.style.display = "block";
  statusText.textContent = "Failed";
  statusStep.textContent = msg;
  spinnerEl.style.display = "none";
  doneIcon.style.display = "none";
  downloadBtn.style.display = "none";
  logPanel.style.display = "block";
}

// ── Service Worker ────────────────────────────────────────────────────────────

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

renderHistory();

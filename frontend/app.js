/* global CONFIG */

const urlInput = document.getElementById("url-input");
const processBtn = document.getElementById("process-btn");
const statusCard = document.getElementById("status-card");
const statusText = document.getElementById("status-text");
const statusStep = document.getElementById("status-step");
const spinnerEl = document.getElementById("spinner");
const doneIcon = document.getElementById("done-icon");
const downloadBtn = document.getElementById("download-btn");
const historyCard = document.getElementById("history-card");
const historyList = document.getElementById("history-list");
const noHistory = document.getElementById("no-history");

let pollTimer = null;
let currentJobId = null;
let notFoundRetries = 0;
const NOT_FOUND_MAX_RETRIES = 6; // 30s grace period for cold starts

const STEP_LABELS = {
  downloading: "Downloading audio…",
  transcribing: "Transcribing with Whisper…",
  translating: "Translating with Claude…",
  building_html: "Building HTML file…",
};

// ── History ──────────────────────────────────────────────────────────────────

function loadHistory() {
  return JSON.parse(localStorage.getItem("job_history") || "[]");
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
    item.innerHTML = `
      <span class="history-title" title="${escHtml(title)}">${escHtml(title)}</span>
      <span class="history-time">${time}</span>
      ${job.downloadUrl
        ? `<a href="${escHtml(job.downloadUrl)}" download class="history-dl" style="background:var(--accent);color:#fff;border-radius:6px;padding:6px 10px;text-decoration:none;font-size:0.8rem;">↓</a>`
        : `<span class="history-time">pending</span>`
      }
    `;
    historyList.appendChild(item);
  });
}

function escHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Process ───────────────────────────────────────────────────────────────────

processBtn.addEventListener("click", async () => {
  const youtubeUrl = urlInput.value.trim();
  if (!youtubeUrl) {
    urlInput.focus();
    return;
  }

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

    saveJob({ jobId: currentJobId, title: youtubeUrl, timestamp: Date.now(), downloadUrl: null });
    startPolling(currentJobId, youtubeUrl);
  } catch (err) {
    showError(err.message);
    processBtn.disabled = false;
  }
});

function startPolling(jobId, originalUrl) {
  clearInterval(pollTimer);
  notFoundRetries = 0;
  pollTimer = setInterval(() => pollStatus(jobId, originalUrl), 5000);
  pollStatus(jobId, originalUrl);
}

async function pollStatus(jobId, originalUrl) {
  try {
    const res = await fetch(`${CONFIG.STATUS_URL}?job_id=${encodeURIComponent(jobId)}`);
    if (!res.ok) return;
    const data = await res.json();

    if (data.status === "done") {
      clearInterval(pollTimer);
      const history = loadHistory();
      const idx = history.findIndex((j) => j.jobId === jobId);
      if (idx >= 0) {
        history[idx].downloadUrl = data.download_url;
        localStorage.setItem("job_history", JSON.stringify(history));
        renderHistory();
      }
      showDone(data.download_url);
      processBtn.disabled = false;
    } else if (data.status === "not_found") {
      notFoundRetries++;
      if (notFoundRetries >= NOT_FOUND_MAX_RETRIES) {
        clearInterval(pollTimer);
        showError("Job not found after multiple retries. Please try again.");
        processBtn.disabled = false;
      } else {
        showStatus("Processing lecture…", "Starting up…", true);
      }
    } else {
      const stepLabel = STEP_LABELS[data.step] || "Processing…";
      showStatus("Processing lecture…", stepLabel, true);
    }
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
  doneIcon.style.display = loading ? "none" : "block";
  downloadBtn.style.display = "none";
}

function showDone(downloadUrl) {
  statusCard.style.display = "block";
  statusText.textContent = "Your lecture is ready!";
  statusStep.textContent = "Tap the button below to download the HTML file.";
  spinnerEl.style.display = "none";
  doneIcon.style.display = "block";
  downloadBtn.style.display = "block";
  downloadBtn.onclick = () => window.location.href = downloadUrl;
}

function showError(msg) {
  statusCard.style.display = "block";
  statusText.textContent = "Error: " + msg;
  statusStep.textContent = "Please check the URL and try again.";
  spinnerEl.style.display = "none";
  doneIcon.style.display = "none";
  downloadBtn.style.display = "none";
}

// ── Service Worker ────────────────────────────────────────────────────────────

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

renderHistory();

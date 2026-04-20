/* global CONFIG */

const urlInput    = document.getElementById("url-input");
const processBtn  = document.getElementById("process-btn");
const statusCard  = document.getElementById("status-card");
const statusText  = document.getElementById("status-text");
const statusStep  = document.getElementById("status-step");
const spinnerEl   = document.getElementById("spinner");
const doneIcon    = document.getElementById("done-icon");
const downloadBtn = document.getElementById("download-btn");
const retryBtn    = document.getElementById("retry-btn");
const logPanel    = document.getElementById("log-panel");
const logToggle   = document.getElementById("log-toggle");
const logOutput   = document.getElementById("log-output");
const libraryCard = document.getElementById("library-card");
const libraryList = document.getElementById("library-list");
const noLibrary   = document.getElementById("no-library");

// Player overlay
const playerOverlay   = document.getElementById("player-overlay");
const lectureFrame    = document.getElementById("lecture-frame");
const playerTitleBar  = document.getElementById("player-title-bar");
const playerDlBtn     = document.getElementById("player-download-btn");
const playerLoading   = document.getElementById("player-loading");
const playerLoadingTxt= document.getElementById("player-loading-text");

let playerJobId = null;
let pollTimer = null;
let currentJobId = null;
let notFoundRetries = 0;
const NOT_FOUND_MAX_RETRIES = 6;

// ── Font size ─────────────────────────────────────────────────────────────────

const FS_KEY = "font_size_pct";
const FS_DEFAULT = 125;

function applyFontSize(pct) {
  document.documentElement.style.fontSize = pct + "%";
  localStorage.setItem(FS_KEY, pct);
  document.querySelectorAll(".fs-btn").forEach(b => {
    b.classList.toggle("active", parseInt(b.dataset.size) === pct);
  });
}

document.querySelectorAll(".fs-btn").forEach(btn => {
  btn.addEventListener("click", () => applyFontSize(parseInt(btn.dataset.size)));
});

applyFontSize(parseInt(localStorage.getItem(FS_KEY) || FS_DEFAULT));

// ── IndexedDB lecture cache ───────────────────────────────────────────────────

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("arabic-player", 1);
    req.onupgradeneeded = e => e.target.result.createObjectStore("lectures", { keyPath: "jobId" });
    req.onsuccess = e => resolve(e.target.result);
    req.onerror = reject;
  });
}

async function idbSave(jobId, html) {
  try {
    const db = await openDB();
    const tx = db.transaction("lectures", "readwrite");
    tx.objectStore("lectures").put({ jobId, html, ts: Date.now() });
  } catch (_) {}
}

async function idbGet(jobId) {
  try {
    const db = await openDB();
    return await new Promise((res, rej) => {
      const req = db.transaction("lectures").objectStore("lectures").get(jobId);
      req.onsuccess = () => res(req.result?.html || null);
      req.onerror = rej;
    });
  } catch (_) { return null; }
}

async function idbDelete(jobId) {
  try {
    const db = await openDB();
    const tx = db.transaction("lectures", "readwrite");
    tx.objectStore("lectures").delete(jobId);
    await new Promise((res, rej) => { tx.oncomplete = res; tx.onerror = rej; });
  } catch (_) {}
}

async function idbGetAll() {
  try {
    const db = await openDB();
    return await new Promise((res) => {
      const items = [];
      const req = db.transaction("lectures").objectStore("lectures").openCursor();
      req.onsuccess = e => {
        const c = e.target.result;
        if (c) { items.push({ jobId: c.key, ts: c.value.ts, size: (c.value.html || "").length }); c.continue(); }
        else res(items);
      };
      req.onerror = () => res([]);
    });
  } catch (_) { return []; }
}

async function idbKeys() {
  const all = await idbGetAll();
  return all.map(x => x.jobId);
}

async function idbClearAll() {
  try {
    const db = await openDB();
    const tx = db.transaction("lectures", "readwrite");
    tx.objectStore("lectures").clear();
    await new Promise((res, rej) => { tx.oncomplete = res; tx.onerror = rej; });
  } catch (_) {}
}

// ── Settings sheet ────────────────────────────────────────────────────────────

const settingsOverlay = document.getElementById("settings-overlay");
const settingsSheet   = document.getElementById("settings-sheet");

document.getElementById("settings-btn").addEventListener("click", () => {
  settingsOverlay.classList.add("open");
  loadCacheList();
});

document.getElementById("settings-close").addEventListener("click", () => {
  settingsOverlay.classList.remove("open");
});

settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) settingsOverlay.classList.remove("open");
});

async function loadCacheList() {
  const cacheListEl  = document.getElementById("cache-list");
  const cacheSizeEl  = document.getElementById("cache-size-hint");
  const items = await idbGetAll();
  if (items.length === 0) {
    cacheSizeEl.textContent = "No lectures cached.";
    cacheListEl.innerHTML = "";
    return;
  }
  const totalMB = (items.reduce((s, x) => s + x.size, 0) / 1024 / 1024).toFixed(1);
  cacheSizeEl.textContent = `${items.length} lecture${items.length !== 1 ? "s" : ""} · ~${totalMB} MB`;

  // Sort newest first
  items.sort((a, b) => (b.ts || 0) - (a.ts || 0));

  // Build list — look up titles from library cache
  let titleMap = {};
  try {
    const cached = localStorage.getItem("library_cache");
    if (cached) {
      const { lectures } = JSON.parse(cached);
      lectures?.forEach(l => { titleMap[l.job_id] = l.title; });
    }
  } catch (_) {}

  cacheListEl.innerHTML = "";
  items.forEach(({ jobId }) => {
    const title = titleMap[jobId] || jobId.slice(0, 8);
    const div = document.createElement("div");
    div.className = "cache-item";
    div.innerHTML = `
      <span class="cache-item-title">${escHtml(title)}</span>
      <button class="btn-cache-remove" data-job="${escHtml(jobId)}">Remove</button>
    `;
    div.querySelector(".btn-cache-remove").addEventListener("click", async (e) => {
      const jid = e.currentTarget.dataset.job;
      await idbDelete(jid);
      showToast("Removed from cache");
      loadCacheList();
      loadLibrary(); // refresh offline badges
    });
    cacheListEl.appendChild(div);
  });
}

document.getElementById("clear-cache-btn").addEventListener("click", async () => {
  await idbClearAll();
  showToast("Cache cleared");
  loadCacheList();
  loadLibrary();
});

// ── Player overlay ────────────────────────────────────────────────────────────

function closePlayer() {
  playerOverlay.classList.remove("open");
  lectureFrame.src = "";
  lectureFrame.style.display = "none";
  playerLoading.classList.remove("visible");
  if (history.state?.player) history.back();
}

window.addEventListener("popstate", () => {
  if (playerOverlay.classList.contains("open")) {
    playerOverlay.classList.remove("open");
    lectureFrame.src = "";
    lectureFrame.style.display = "none";
    playerLoading.classList.remove("visible");
    if (playParam) window.location.href = "/";
  }
});

// Tap the title to rename
playerTitleBar.addEventListener("click", () => {
  if (!playerJobId || playerTitleBar.dataset.shared) return;
  const current = playerTitleBar.textContent;
  const newTitle = prompt("Rename lecture:", current);
  if (!newTitle || newTitle.trim() === current) return;
  const trimmed = newTitle.trim();
  fetch(CONFIG.RENAME_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: playerJobId, title: trimmed }),
  }).then(r => r.json()).then(data => {
    if (data.error) { showToast("Rename failed: " + data.error, 3000); return; }
    playerTitleBar.textContent = trimmed;
    try {
      const cached = localStorage.getItem("library_cache");
      if (cached) {
        const obj = JSON.parse(cached);
        const lec = obj.lectures?.find(l => l.job_id === playerJobId);
        if (lec) { lec.title = trimmed; localStorage.setItem("library_cache", JSON.stringify(obj)); }
      }
    } catch (_) {}
    showToast("Renamed!");
    loadLibrary();
  }).catch(() => showToast("Rename failed", 3000));
});

async function openPlayer(jobId, title) {
  playerJobId = jobId;
  playerTitleBar.textContent = title || jobId.slice(0, 8);
  playerDlBtn.href = `${CONFIG.DOWNLOAD_URL}?job_id=${encodeURIComponent(jobId)}&dl=1`;
  playerDlBtn.download = `lecture-${jobId.slice(0, 8)}.html`;

  // Show loading state immediately
  lectureFrame.style.display = "none";
  lectureFrame.src = "";
  playerLoading.classList.add("visible");
  playerLoadingTxt.textContent = "Loading lecture…";
  playerOverlay.classList.add("open");
  history.pushState({ player: true }, "");

  try {
    let html = await idbGet(jobId);
    if (html) {
      playerLoadingTxt.textContent = "Loading from cache…";
    } else {
      playerLoadingTxt.textContent = "Downloading lecture… (this may take a moment)";
      const res = await fetch(`${CONFIG.DOWNLOAD_URL}?job_id=${encodeURIComponent(jobId)}`);
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      html = await res.text();
      idbSave(jobId, html);
    }
    const blob = new Blob([html], { type: "text/html" });
    lectureFrame.src = URL.createObjectURL(blob);
    lectureFrame.style.display = "block";
    playerLoading.classList.remove("visible");
    playerTitleBar.textContent = title || jobId.slice(0, 8);
  } catch (err) {
    playerLoading.classList.remove("visible");
    lectureFrame.style.display = "block";
    playerTitleBar.textContent = "Failed to load — " + err.message;
  }
}

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
  if (!logOpen) {
    logOpen = true;
    logOutput.style.display = "block";
    logToggle.textContent = "▼ Show live log";
  }
  logOutput.textContent = logs.map((e) => `[${String(e.t).padStart(6)}s] ${e.msg}`).join("\n");
  logOutput.scrollTop = logOutput.scrollHeight;
}

const STEP_LABELS = {
  waiting_download: "Waiting for your PC to download the audio…",
  downloading:      "Downloading audio on your PC…",
  queued:           "Queued, waiting for worker…",
  transcribing:     "Transcribing with Whisper…",
  translating:      "Translating with Claude…",
  building_html:    "Building HTML file…",
};

// ── Library (server-side) ─────────────────────────────────────────────────────

async function loadLibrary() {
  const cached = localStorage.getItem("library_cache");
  if (cached) {
    try {
      const { lectures, totals, month_totals, year_totals } = JSON.parse(cached);
      renderLibrary(lectures, totals, month_totals, year_totals);
    } catch (_) {}
  }
  try {
    const res = await fetch(CONFIG.LIBRARY_URL);
    if (!res.ok) return;
    const data = await res.json();
    localStorage.setItem("library_cache", JSON.stringify(data));
    renderLibrary(data.lectures, data.totals, data.month_totals, data.year_totals);
  } catch (_) {}
}

async function renderLibrary(lectures, totals, month_totals, year_totals) {
  const cachedIds = new Set(await idbKeys());
  if (!lectures || lectures.length === 0) {
    noLibrary.style.display = "block";
    libraryCard.style.display = "none";
    return;
  }
  noLibrary.style.display = "none";
  libraryCard.style.display = "block";
  libraryList.innerHTML = "";

  if (totals && totals.count > 0) {
    const now = new Date();
    const monthName = now.toLocaleString("default", { month: "long" });
    const year = now.getFullYear();
    const fmtRow = (label, t) => t && t.count > 0
      ? `<span>${label}: <strong>$${t.total_usd.toFixed(3)}</strong> (${t.count} lecture${t.count !== 1 ? "s" : ""})</span>`
      : "";
    const summary = document.createElement("div");
    summary.className = "cost-summary";
    summary.innerHTML =
      `<strong>All time:</strong> $${totals.total_usd.toFixed(3)} · ${totals.count} lecture${totals.count !== 1 ? "s" : ""}<br>` +
      (fmtRow(monthName, month_totals) ? fmtRow(monthName, month_totals) + "<br>" : "") +
      (fmtRow(String(year), year_totals) ? fmtRow(String(year), year_totals) : "");
    libraryList.appendChild(summary);
  }

  lectures.forEach((lec) => {
    const item = document.createElement("div");
    item.className = "library-item";
    const date  = lec.timestamp ? new Date(lec.timestamp * 1000).toLocaleDateString() : "";
    const title = escHtml(lec.title || lec.job_id.slice(0, 8));
    const dlUrl = `${CONFIG.DOWNLOAD_URL}?job_id=${encodeURIComponent(lec.job_id)}&dl=1`;
    const cost    = lec.costs?.total_usd != null ? ` · $${lec.costs.total_usd.toFixed(3)}` : "";
    const offline = cachedIds.has(lec.job_id) ? ' <span title="Available offline">📥</span>' : "";

    item.innerHTML = `
      <div class="library-info">
        <span class="library-title" title="${title}">${title}${offline}</span>
        <span class="library-date">${date}${cost}</span>
      </div>
      <div class="library-actions">
        <button class="btn-play"  data-job="${escHtml(lec.job_id)}" data-title="${title}">▶ Play</button>
        <button class="btn-share" data-job="${escHtml(lec.job_id)}" data-title="${title}" title="Share">⤴</button>
        <a class="btn-save" href="${escHtml(dlUrl)}" download="lecture-${lec.job_id.slice(0, 8)}.html" title="Download">↓</a>
      </div>
    `;

    item.querySelector(".btn-play").addEventListener("click", (e) => {
      openPlayer(e.currentTarget.dataset.job, e.currentTarget.dataset.title);
    });

    item.querySelector(".btn-share").addEventListener("click", (e) => {
      shareLecture(e.currentTarget.dataset.job, e.currentTarget.dataset.title);
    });

    libraryList.appendChild(item);
  });
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── YouTube URL submit ────────────────────────────────────────────────────────

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
    startPolling(currentJobId, youtubeUrl, true);
  } catch (err) {
    showError(err.message);
    processBtn.disabled = false;
  }
});

// ── Direct upload ─────────────────────────────────────────────────────────────

document.getElementById("audio-file")?.addEventListener("change", (e) => {
  const f = e.target.files[0];
  document.getElementById("upload-label").textContent = f ? `🎵 ${f.name}` : "📂 Tap to pick an audio file";
});

document.getElementById("upload-btn")?.addEventListener("click", async () => {
  const fileInput  = document.getElementById("audio-file");
  const progressEl = document.getElementById("upload-progress");
  const uploadBtn  = document.getElementById("upload-btn");
  const file = fileInput.files[0];
  if (!file) { fileInput.click(); return; }

  const CHUNK = 38 * 1024 * 1024;
  const totalChunks = Math.ceil(file.size / CHUNK);
  const sizeMB = (file.size / 1024 / 1024).toFixed(1);
  const title = file.name.replace(/\.[^.]+$/, "");
  const jobId = crypto.randomUUID();

  uploadBtn.disabled = true;
  progressEl.style.display = "block";
  showStatus("Uploading audio…", `${sizeMB} MB${totalChunks > 1 ? ` · ${totalChunks} chunks` : ""}`, true);

  try {
    for (let i = 0; i < totalChunks; i++) {
      progressEl.textContent = totalChunks > 1
        ? `Uploading chunk ${i + 1} of ${totalChunks}…`
        : `Uploading ${file.name} (${sizeMB} MB)…`;

      const chunk = file.slice(i * CHUNK, (i + 1) * CHUNK);
      const form = new FormData();
      form.append("audio", chunk, file.name);
      form.append("title", title);
      form.append("job_id", jobId);
      form.append("chunk_index", i);
      form.append("total_chunks", totalChunks);

      const res = await fetch(CONFIG.UPLOAD_URL, { method: "POST", body: form });
      if (!res.ok) throw Object.assign(new Error(`Upload failed: ${res.status}`), { status: res.status });
      const data = await res.json();
      if (data.status === "processing" && data.job_id) {
        currentJobId = data.job_id;
      }
    }
    progressEl.style.display = "none";
    startPolling(currentJobId || jobId, title, true);
  } catch (err) {
    showError(friendlyError(err.message, err.status));
    progressEl.style.display = "none";
  }
  uploadBtn.disabled = false;
});

// ── Error messages ────────────────────────────────────────────────────────────

function friendlyError(msg, httpStatus) {
  if (httpStatus === 413) return "File too large — Modal's limit is ~50 MB. Try a shorter clip, or use the YouTube URL option instead.";
  if (httpStatus === 422) return "Server rejected the request (422) — try refreshing and submitting again.";
  if (httpStatus === 500) return "Server error (500) — the processing worker crashed. Check the live log for details.";
  if (httpStatus === 503) return "Service unavailable (503) — Modal may be starting up. Wait 30 s and try again.";
  if (msg.includes("Failed to fetch") || msg.includes("NetworkError")) return "No internet connection — check your network and try again.";
  return msg;
}

// ── Polling ───────────────────────────────────────────────────────────────────

const ACTIVE_JOB_KEY = "active_job";

function saveActiveJob(jobId, title, status = "processing") {
  localStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify({ jobId, title, status }));
}

function clearActiveJob() {
  localStorage.removeItem(ACTIVE_JOB_KEY);
}

function startPolling(jobId, title, isNewJob = false) {
  clearInterval(pollTimer);
  notFoundRetries = 0;
  saveActiveJob(jobId, title, "processing");
  pollTimer = setInterval(() => pollStatus(jobId, title, isNewJob), 5000);
  pollStatus(jobId, title, isNewJob);
}

async function pollStatus(jobId, title, isNewJob = false) {
  try {
    const res = await fetch(`${CONFIG.STATUS_URL}?job_id=${encodeURIComponent(jobId)}`);
    if (!res.ok) return;
    const data = await res.json();

    if (data.status === "done") {
      clearInterval(pollTimer);
      clearActiveJob();
      const resolvedTitle = data.title || title;
      showDone(data.download_url, jobId, resolvedTitle);
      processBtn.disabled = false;
      loadLibrary();

    } else if (data.status === "failed") {
      clearInterval(pollTimer);
      // Keep active job so retry works after refresh
      saveActiveJob(jobId, title, "failed");
      const errShort = (data.error || "").split("\n")[0];
      showError(friendlyError(errShort, null), jobId, title);
      processBtn.disabled = false;

    } else if (data.status === "not_found") {
      notFoundRetries++;
      if (!isNewJob || notFoundRetries >= NOT_FOUND_MAX_RETRIES) {
        clearInterval(pollTimer);
        clearActiveJob();
        showError("Job not found — it may have expired.");
        processBtn.disabled = false;
      } else {
        showStatus("Processing lecture…", "Starting up…", true);
      }

    } else {
      const stepLabel = STEP_LABELS[data.step] || "Processing…";
      showStatus("Processing lecture…", stepLabel, true);
    }

    renderLogs(data.logs);
  } catch (_) {}
}

// ── Retry ─────────────────────────────────────────────────────────────────────

retryBtn.addEventListener("click", async () => {
  const jobId = retryBtn.dataset.job;
  const title = retryBtn.dataset.title;
  if (!jobId) return;

  retryBtn.disabled = true;
  showStatus("Retrying…", "Resuming from checkpoint if available…", true);

  try {
    const res = await fetch(CONFIG.RETRY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    startPolling(jobId, title, false);
  } catch (err) {
    showError("Retry failed: " + err.message, jobId, title);
    retryBtn.disabled = false;
  }
});

// ── UI helpers ────────────────────────────────────────────────────────────────

function showStatus(main, sub, loading) {
  statusCard.style.display = "block";
  statusText.textContent = main;
  statusStep.textContent = sub;
  spinnerEl.style.display = loading ? "block" : "none";
  doneIcon.style.display = "none";
  downloadBtn.style.display = "none";
  retryBtn.style.display = "none";
  logPanel.style.display = "none";
  logOutput.textContent = "";
  logOpen = false;
  logOutput.style.display = "none";
  logToggle.textContent = "▶ Show live log";
}

function showDone(downloadUrl, jobId, title) {
  statusCard.style.display = "block";
  statusText.textContent = "Your lecture is ready!";
  statusStep.textContent = "";
  spinnerEl.style.display = "none";
  doneIcon.style.display = "block";
  downloadBtn.style.display = "block";
  retryBtn.style.display = "none";
  downloadBtn.textContent = "▶ Open Lecture";
  downloadBtn.onclick = () => openPlayer(jobId, title);
}

function showError(msg, jobId, title) {
  statusCard.style.display = "block";
  statusText.textContent = "Failed";
  statusStep.textContent = msg;
  spinnerEl.style.display = "none";
  doneIcon.style.display = "none";
  downloadBtn.style.display = "none";
  logPanel.style.display = "block";
  logOpen = true;
  logOutput.style.display = "block";
  logToggle.textContent = "▼ Show live log";

  if (jobId) {
    retryBtn.style.display = "block";
    retryBtn.dataset.job = jobId;
    retryBtn.dataset.title = title || "";
    retryBtn.disabled = false;
  } else {
    retryBtn.style.display = "none";
  }
}

// ── Service Worker ────────────────────────────────────────────────────────────

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

// ── Share ─────────────────────────────────────────────────────────────────────

async function shareLecture(jobId, title) {
  const url = `${location.origin}/?play=${encodeURIComponent(jobId)}`;
  if (navigator.share) {
    try {
      await navigator.share({ title: title || "Arabic Lecture", url });
      return;
    } catch (_) {}
  }
  try {
    await navigator.clipboard.writeText(url);
    showToast("Link copied!");
  } catch (_) {
    showToast(url, 5000);
  }
}

function showToast(msg, duration = 2000) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), duration);
}

// ── Rename ────────────────────────────────────────────────────────────────────

function startRename(item, jobId, currentTitle) {
  const titleSpan = item.querySelector(".library-title");
  const renameBtn = item.querySelector(".btn-rename");
  const originalHTML = titleSpan.innerHTML;

  renameBtn.disabled = true;
  titleSpan.innerHTML = "";

  const input = document.createElement("input");
  input.type = "text";
  input.value = currentTitle;
  input.style.cssText = "flex:1;min-width:0;font-size:0.88rem;padding:4px 8px;border-radius:6px;background:#242424;color:#e0e0e0;border:1px solid var(--accent);outline:none;";
  titleSpan.appendChild(input);
  input.focus();
  input.select();

  async function save() {
    const newTitle = input.value.trim();
    if (!newTitle || newTitle === currentTitle) {
      titleSpan.innerHTML = originalHTML;
      renameBtn.disabled = false;
      return;
    }
    renameBtn.disabled = true;
    try {
      const res = await fetch(CONFIG.RENAME_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId, title: newTitle }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      try {
        const cached = localStorage.getItem("library_cache");
        if (cached) {
          const obj = JSON.parse(cached);
          const lec = obj.lectures?.find(l => l.job_id === jobId);
          if (lec) { lec.title = newTitle; localStorage.setItem("library_cache", JSON.stringify(obj)); }
        }
      } catch (_) {}

      item.querySelectorAll("[data-title]").forEach(el => el.dataset.title = newTitle);
      titleSpan.textContent = newTitle;
      showToast("Renamed!");
    } catch (err) {
      titleSpan.innerHTML = originalHTML;
      showToast("Rename failed: " + err.message, 3000);
    }
    renameBtn.disabled = false;
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { titleSpan.innerHTML = originalHTML; renameBtn.disabled = false; }
  });
  input.addEventListener("blur", save);
}

// ── Init ──────────────────────────────────────────────────────────────────────

const playParam = new URLSearchParams(location.search).get("play");

if (!playParam) {
  // Resume any in-progress or failed job after refresh
  try {
    const saved = localStorage.getItem(ACTIVE_JOB_KEY);
    if (saved) {
      const { jobId, title, status } = JSON.parse(saved);
      if (jobId) {
        currentJobId = jobId;
        if (status === "failed") {
          // Show retry UI immediately without polling
          showError("Previous processing failed. Click Retry to resume from checkpoint.", jobId, title);
        } else {
          showStatus("Resuming…", "Checking job status…", true);
          startPolling(jobId, title, false);
        }
      }
    }
  } catch (_) {}
}

if (playParam) {
  document.getElementById("main-view").style.display = "none";
  playerTitleBar.dataset.shared = "1";

  (async () => {
    let title = "";
    try {
      const res = await fetch(`${CONFIG.STATUS_URL}?job_id=${encodeURIComponent(playParam)}`);
      if (res.ok) {
        const data = await res.json();
        title = data.title || "";
      }
    } catch (_) {}
    openPlayer(playParam, title || playParam.slice(0, 8));
  })();
} else {
  loadLibrary();
}

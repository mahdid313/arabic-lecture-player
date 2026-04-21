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
const cancelBtn   = document.getElementById("cancel-btn");
const statusTitle = document.getElementById("status-title");
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
        if (c) {
          const html = c.value.html || "";
          const titleMatch = html.match(/<title>([^<]*)<\/title>/i);
          const title = c.value.title || (titleMatch ? titleMatch[1] : null);
          items.push({ jobId: c.key, ts: c.value.ts, size: html.length, title });
          c.continue();
        } else res(items);
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

  const downloadUrl = `${CONFIG.DOWNLOAD_URL}?job_id=${encodeURIComponent(jobId)}`;

  function showFrame() {
    lectureFrame.style.display = "block";
    playerLoading.classList.remove("visible");
  }

  // lectureReady fires from inside the iframe once static transcript text is painted.
  function onLectureReady(e) {
    if (e.data?.type === "lectureReady") {
      window.removeEventListener("message", onLectureReady);
      showFrame();
    }
  }
  window.addEventListener("message", onLectureReady);

  try {
    const cached = await idbGet(jobId);
    if (cached) {
      playerLoadingTxt.textContent = "Loading from cache…";
      const blob = new Blob([cached], { type: "text/html" });
      lectureFrame.src = URL.createObjectURL(blob);
      lectureFrame.onload = showFrame; // blob loads instantly; postMessage also fires
    } else {
      // Stream from server with progress bar, then set a blob URL.
      // Blob URLs never add to the browser's session history (unlike external URLs),
      // so back always returns home in one swipe.
      playerLoadingTxt.textContent = "Connecting…";
      const res = await fetch(downloadUrl);
      if (!res.ok) throw new Error(`Server returned ${res.status}`);

      const total = parseInt(res.headers.get("Content-Length") || "0", 10);
      const reader = res.body.getReader();
      const chunks = [];
      let received = 0;
      const startTime = Date.now();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.length;
        const elapsed = (Date.now() - startTime) / 1000 || 0.001;
        const speed = received / elapsed;
        if (total) {
          const pct = Math.min(99, Math.round(received / total * 100));
          const secsLeft = Math.ceil((total - received) / speed);
          const timeStr = secsLeft > 60 ? `${Math.ceil(secsLeft / 60)}m left` : `${secsLeft}s left`;
          playerLoadingTxt.textContent = `Downloading… ${pct}% · ${timeStr}`;
        } else {
          playerLoadingTxt.textContent = `Downloading… ${(received / 1024 / 1024).toFixed(1)} MB`;
        }
      }

      const html = await new Blob(chunks).text();
      idbSave(jobId, html);
      const blob = new Blob([html], { type: "text/html" });
      lectureFrame.src = URL.createObjectURL(blob);
      // Static HTML renders instantly from blob; postMessage + onload both fire fast
      lectureFrame.onload = showFrame;
    }
    playerTitleBar.textContent = title || jobId.slice(0, 8);
  } catch (err) {
    window.removeEventListener("message", onLectureReady);
    showFrame();
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
  const cachedItems = await idbGetAll();
  const cachedIds = new Set(cachedItems.map(x => x.jobId));
  const serverIds = new Set((lectures || []).map(l => l.job_id));

  // Lectures only in IDB (server doesn't know about them)
  const offlineOnly = cachedItems.filter(x => !serverIds.has(x.jobId));

  const allEmpty = (!lectures || lectures.length === 0) && offlineOnly.length === 0;
  if (allEmpty) {
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

  const makeLibraryItem = (jobId, title, date, cost, isCached, isOfflineOnly) => {
    const item = document.createElement("div");
    item.className = "library-item";
    const offline = isCached ? " 📥" : "";
    const rawTitle = title || jobId.slice(0, 8);
    const safeTitle = escHtml(rawTitle);
    item.innerHTML = `
      <div class="library-info">
        <span class="library-title">${safeTitle}${offline}</span>
        <span class="library-date">${escHtml(date)}${cost}${isOfflineOnly ? " · offline only" : ""}</span>
      </div>
      <div class="library-actions">
        <button class="btn-play" data-job="${escHtml(jobId)}" data-title="${safeTitle}">▶ Play</button>
        <button class="btn-options" data-job="${escHtml(jobId)}" data-title="${safeTitle}"
                data-cached="${isCached}" data-offline="${isOfflineOnly}" title="Options">⋯</button>
      </div>
    `;
    item.querySelector(".btn-play").addEventListener("click", (e) => {
      openPlayer(e.currentTarget.dataset.job, e.currentTarget.dataset.title);
    });
    item.querySelector(".btn-options").addEventListener("click", (e) => {
      const b = e.currentTarget;
      openLectureOptions(b.dataset.job, b.dataset.title,
        b.dataset.cached === "true", b.dataset.offline === "true");
    });
    return item;
  };

  lectures.forEach((lec) => {
    const date  = lec.timestamp ? new Date(lec.timestamp * 1000).toLocaleDateString() : "";
    const title = lec.title || lec.job_id.slice(0, 8);
    const cost  = lec.costs?.total_usd != null ? ` · $${lec.costs.total_usd.toFixed(3)}` : "";
    libraryList.appendChild(makeLibraryItem(lec.job_id, title, date, cost, cachedIds.has(lec.job_id), false));
  });

  if (offlineOnly.length > 0) {
    const sep = document.createElement("div");
    sep.style.cssText = "font-size:0.75rem;color:#555;margin:10px 0 4px;padding-top:6px;border-top:1px solid #222;";
    sep.textContent = "Cached locally:";
    libraryList.appendChild(sep);
    offlineOnly.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    offlineOnly.forEach(({ jobId, title, ts }) => {
      const date = ts ? new Date(ts).toLocaleDateString() : "";
      libraryList.appendChild(makeLibraryItem(jobId, title, date, "", true, true));
    });
  }
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Per-lecture options sheet ─────────────────────────────────────────────────

const lecOptOverlay = document.getElementById("lec-opt-overlay");
const lecOptSheet   = document.getElementById("lec-opt-sheet");
const lecOptTitle   = document.getElementById("lec-opt-title");
const lecOptBody    = document.getElementById("lec-opt-body");

document.getElementById("lec-opt-close").addEventListener("click", closeLecOpt);
lecOptOverlay.addEventListener("click", (e) => { if (e.target === lecOptOverlay) closeLecOpt(); });

function closeLecOpt() { lecOptOverlay.classList.remove("open"); }

function optBtn(icon, label, cls, onClick) {
  const b = document.createElement("button");
  b.className = "lec-opt-btn" + (cls ? " " + cls : "");
  b.innerHTML = `<span class="lec-opt-icon">${icon}</span>${escHtml(label)}`;
  b.addEventListener("click", () => { closeLecOpt(); onClick(); });
  return b;
}

async function openLectureOptions(jobId, title, isCached, isOfflineOnly) {
  lecOptTitle.textContent = title || jobId.slice(0, 8);
  lecOptBody.innerHTML = "";

  const dlUrl = `${CONFIG.DOWNLOAD_URL}?job_id=${encodeURIComponent(jobId)}&dl=1`;

  // Play
  lecOptBody.appendChild(optBtn("▶", "Play", "", () => openPlayer(jobId, title)));

  // Share
  lecOptBody.appendChild(optBtn("⤴", "Share", "", () => shareLecture(jobId, title)));

  // Rename
  lecOptBody.appendChild(optBtn("✏️", "Rename", "", () => {
    const newTitle = prompt("Rename lecture:", title);
    if (!newTitle || newTitle.trim() === title) return;
    const trimmed = newTitle.trim();
    fetch(CONFIG.RENAME_URL, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, title: trimmed }),
    }).then(r => r.json()).then(d => {
      if (d.error) { showToast("Rename failed: " + d.error, 3000); return; }
      // Update local library cache
      try {
        const c = localStorage.getItem("library_cache");
        if (c) {
          const obj = JSON.parse(c);
          const lec = obj.lectures?.find(l => l.job_id === jobId);
          if (lec) { lec.title = trimmed; localStorage.setItem("library_cache", JSON.stringify(obj)); }
        }
      } catch (_) {}
      showToast("Renamed!");
      loadLibrary();
    }).catch(() => showToast("Rename failed", 3000));
  }));

  // Reprocess translation (only for server-side lectures)
  if (!isOfflineOnly) {
    lecOptBody.appendChild(optBtn("🔄", "Reprocess Translation", "", () => {
      if (!confirm("This will re-translate the lecture from scratch using the current prompt. Continue?")) return;
      // Show status card immediately so the user sees progress
      showStatus("Reprocessing translation…", "Waiting for worker…", true, title);
      statusCard.scrollIntoView({ behavior: "smooth", block: "start" });
      fetch(CONFIG.RETRY_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId, mode: "retranslate" }),
      }).then(r => r.json()).then(d => {
        if (d.error) { showError("Error: " + d.error); return; }
        startPolling(jobId, title, false);
      }).catch(() => showError("Request failed — check your connection"));
    }));
  }

  // Download HTML
  if (!isOfflineOnly) {
    const a = document.createElement("a");
    a.className = "lec-opt-btn";
    a.href = dlUrl;
    a.download = `lecture-${jobId.slice(0, 8)}.html`;
    a.innerHTML = `<span class="lec-opt-icon">↓</span>Download HTML file`;
    a.addEventListener("click", closeLecOpt);
    lecOptBody.appendChild(a);
  }

  // Remove from cache
  if (isCached) {
    lecOptBody.appendChild(optBtn("🗑", "Remove from Cache", "muted", async () => {
      await idbDelete(jobId);
      showToast("Removed from cache");
      loadLibrary();
    }));
  }

  // Delete permanently
  if (!isOfflineOnly) {
    lecOptBody.appendChild(optBtn("⚠️", "Delete Permanently", "danger", () => {
      if (!confirm(`Permanently delete "${title}"? This cannot be undone.`)) return;
      fetch(CONFIG.RENAME_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId, action: "delete" }),
      }).then(r => r.json()).then(d => {
        if (d.error) { showToast("Delete failed: " + d.error, 3000); return; }
        idbDelete(jobId);
        // Remove from local cache
        try {
          const c = localStorage.getItem("library_cache");
          if (c) {
            const obj = JSON.parse(c);
            obj.lectures = obj.lectures?.filter(l => l.job_id !== jobId);
            localStorage.setItem("library_cache", JSON.stringify(obj));
          }
        } catch (_) {}
        showToast("Deleted");
        loadLibrary();
      }).catch(() => showToast("Delete failed", 3000));
    }));
  } else {
    // Offline-only: just remove from cache
    lecOptBody.appendChild(optBtn("⚠️", "Remove from Cache", "danger", async () => {
      if (!confirm("Remove this lecture from local cache?")) return;
      await idbDelete(jobId);
      showToast("Removed");
      loadLibrary();
    }));
  }

  lecOptOverlay.classList.add("open");
}

// ── YouTube URL submit ────────────────────────────────────────────────────────

processBtn.addEventListener("click", async () => {
  const youtubeUrl = urlInput.value.trim();
  if (!youtubeUrl) { urlInput.focus(); return; }

  processBtn.disabled = true;
  showStatus("Submitting…", "", true, youtubeUrl);

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
  showStatus("Uploading audio…", `${sizeMB} MB${totalChunks > 1 ? ` · ${totalChunks} chunks` : ""}`, true, title);

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
  const pollStart = Date.now();
  pollTimer = setInterval(() => pollStatus(jobId, title, isNewJob, pollStart), 5000);
  pollStatus(jobId, title, isNewJob, pollStart);
}

async function pollStatus(jobId, title, isNewJob = false, pollStart = Date.now()) {
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
      const displayTitle = data.title || data.youtube_url || title || "";
      const elapsedMin = (Date.now() - pollStart) / 60000;
      if (elapsedMin > 12) {
        // Job likely timed out server-side without writing a failure status
        clearInterval(pollTimer);
        saveActiveJob(jobId, title, "failed");
        showError("Processing timed out — click Retry to resume from checkpoint.", jobId, title);
        processBtn.disabled = false;
      } else {
        showStatus("Processing lecture…", stepLabel, true, displayTitle);
      }
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

// ── Cancel ────────────────────────────────────────────────────────────────────

cancelBtn.addEventListener("click", () => {
  clearInterval(pollTimer);
  clearActiveJob();
  statusCard.style.display = "none";
  processBtn.disabled = false;
  showToast("Cancelled");
});

// ── UI helpers ────────────────────────────────────────────────────────────────

function showStatus(main, sub, loading, title) {
  statusCard.style.display = "block";
  statusText.textContent = main;
  statusStep.textContent = sub;
  spinnerEl.style.display = loading ? "block" : "none";
  doneIcon.style.display = "none";
  downloadBtn.style.display = "none";
  retryBtn.style.display = "none";
  cancelBtn.style.display = loading ? "block" : "none";
  logPanel.style.display = "none";
  logOutput.textContent = "";
  logOpen = false;
  logOutput.style.display = "none";
  logToggle.textContent = "▶ Show live log";
  if (title) {
    statusTitle.textContent = title;
    statusTitle.style.display = "block";
  } else {
    statusTitle.style.display = "none";
  }
}

function showDone(downloadUrl, jobId, title) {
  statusCard.style.display = "block";
  statusText.textContent = "Your lecture is ready!";
  statusStep.textContent = "";
  spinnerEl.style.display = "none";
  doneIcon.style.display = "block";
  downloadBtn.style.display = "block";
  retryBtn.style.display = "none";
  cancelBtn.style.display = "none";
  statusTitle.textContent = title;
  statusTitle.style.display = title ? "block" : "none";
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
  cancelBtn.style.display = "none";
  statusTitle.style.display = "none";
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

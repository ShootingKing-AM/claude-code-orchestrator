"use strict";

// ── State ────────────────────────────────────────────────────────────────────

let activeJobId = null;

// ── Unread tracking ───────────────────────────────────────────────────────────
const _seenSeq = (() => {
  try { return JSON.parse(localStorage.getItem("orch_seen_seq") || "{}"); }
  catch { return {}; }
})();
function _saveSeenSeq() {
  try { localStorage.setItem("orch_seen_seq", JSON.stringify(_seenSeq)); } catch {}
}
function markJobRead(jobId, seq) {
  if ((_seenSeq[jobId] || 0) < seq) { _seenSeq[jobId] = seq; _saveSeenSeq(); }
}
function hasUnread(job) {
  return (job.last_seq || 0) > (_seenSeq[job.id] || 0);
}
let activeJob   = null;
let evtSource   = null;
let seqCount    = 0;
const _renderedSeqs    = new Set();
const _seenSessionIds  = new Set();
const _pendingUserMsgs = new Set();

// ── DOM refs ─────────────────────────────────────────────────────────────────

const jobList         = document.getElementById("job-list");
const outputWrap      = document.getElementById("output-wrap");
const emptyState      = document.getElementById("empty-state");
const cancelBtn       = document.getElementById("cancel-btn");
const statusDot       = document.getElementById("status-dot");
const statusText      = document.getElementById("status-text");
const statusSeq       = document.getElementById("status-seq");
const modalOverlay    = document.getElementById("modal-overlay");
const promptInput     = document.getElementById("prompt-input");
const cwdInput        = document.getElementById("cwd-input");
const detailEmpty     = document.getElementById("detail-empty");
const detailContent   = document.getElementById("detail-content");
const detailPrompt    = document.getElementById("detail-prompt");
const metaState       = document.getElementById("meta-state");
const metaId          = document.getElementById("meta-id");
const metaCreated     = document.getElementById("meta-created");
const metaUpdated     = document.getElementById("meta-updated");
const metaCwd         = document.getElementById("meta-cwd");
const metaCwdWrap     = document.getElementById("meta-cwd-wrap");
const metaSession     = document.getElementById("meta-session");
const metaSessionWrap = document.getElementById("meta-session-wrap");
const metaResumes     = document.getElementById("meta-resumes");
const metaResumesWrap = document.getElementById("meta-resumes-wrap");
const metaError       = document.getElementById("meta-error");
const metaErrorWrap   = document.getElementById("meta-error-wrap");
const msgBar              = document.getElementById("msg-bar");
const msgInput            = document.getElementById("msg-input");
const msgSend             = document.getElementById("msg-send");
const thinkingIndicator   = document.getElementById("thinking-indicator");
const thinkingLabel       = document.getElementById("thinking-label");
const titleInput      = document.getElementById("title-input");
const detailTitle     = document.getElementById("detail-title");
const editTitleBtn    = document.getElementById("edit-title-btn");
const editTitleInput  = document.getElementById("edit-title-input");
const metaTokensWrap  = document.getElementById("meta-tokens-wrap");
const metaTokens      = document.getElementById("meta-tokens");
const statSpawned     = document.getElementById("stat-spawned");
const statRunning     = document.getElementById("stat-running");
const statInTok       = document.getElementById("stat-in-tok");
const statOutTok      = document.getElementById("stat-out-tok");
const sidebarToggle   = document.getElementById("sidebar-toggle");
const sidebar         = document.getElementById("sidebar");

// ── Sidebar collapse ──────────────────────────────────────────────────────────

(function initSidebar() {
  if (localStorage.getItem("sidebar-collapsed") === "1") {
    sidebar.classList.add("collapsed");
  }
  sidebarToggle.addEventListener("click", () => {
    const collapsed = sidebar.classList.toggle("collapsed");
    localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0");
  });
})();

// ── Attachments helper ────────────────────────────────────────────────────────

class Attachments {
  constructor(chipsEl, inputEl, fileInputEl) {
    this._chips = chipsEl;       // .attachment-chips container
    this._input = inputEl;       // textarea or text input to highlight on drag
    this._fileInput = fileInputEl; // hidden <input type="file">
    this._items = [];            // [{ name, path, chipEl }]

    this._input.addEventListener("dragover",  e => this._onDragOver(e));
    this._input.addEventListener("dragleave", e => this._onDragLeave(e));
    this._input.addEventListener("drop",      e => this._onDrop(e));
    this._input.addEventListener("paste",     e => this._onPaste(e));
    this._fileInput.addEventListener("change", e => this._onFileInput(e));
  }

  // Public: open native file picker
  openPicker() { this._fileInput.value = ""; this._fileInput.click(); }

  // Public: return paths to inject into prompt, then clear
  consumePaths() {
    const paths = this._items.filter(i => i.path).map(i => i.path);
    this.clear();
    return paths;
  }

  // Public: clear all chips and state
  clear() {
    this._items = [];
    this._chips.innerHTML = "";
    this._chips.classList.remove("has-chips");
  }

  _onDragOver(e) {
    if (!e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    this._input.classList.add("drag-over");
  }

  _onDragLeave(e) { this._input.classList.remove("drag-over"); }

  _onDrop(e) {
    e.preventDefault();
    this._input.classList.remove("drag-over");
    const files = Array.from(e.dataTransfer.files);
    if (files.length) this._uploadFiles(files);
  }

  _onPaste(e) {
    const files = Array.from(e.clipboardData.files || []);
    if (files.length) { e.preventDefault(); this._uploadFiles(files); }
  }

  _onFileInput(e) {
    const files = Array.from(e.target.files || []);
    if (files.length) this._uploadFiles(files);
  }

  _uploadFiles(files) {
    for (const file of files) {
      const chipEl = this._addChip(file.name, "uploading");
      const item = { name: file.name, path: null, chipEl };
      this._items.push(item);

      const fd = new FormData();
      fd.append("files", file, file.name);

      fetch("/api/upload", { method: "POST", body: fd })
        .then(r => r.ok ? r.json() : Promise.reject(r.status))
        .then(results => {
          if (results.length) {
            item.path = results[0].path;
            chipEl.classList.remove("uploading");
            const removeBtn = chipEl.querySelector(".attach-chip-remove");
            if (removeBtn) removeBtn.style.display = "";
          } else {
            throw new Error("empty");
          }
        })
        .catch(() => {
          item.path = null;
          chipEl.classList.remove("uploading");
          chipEl.classList.add("error");
          chipEl.querySelector(".attach-chip-icon").textContent = "📎";
          const nameEl = chipEl.querySelector(".attach-chip-name");
          if (nameEl) nameEl.textContent = "upload failed";
          setTimeout(() => {
            this._removeChip(item);
          }, 3000);
        });
    }
  }

  _addChip(name, state) {
    const displayName = name.length > 30 ? name.slice(0, 27) + "…" : name;
    const chip = document.createElement("span");
    chip.className = "attach-chip" + (state === "uploading" ? " uploading" : "");
    chip.innerHTML = `
      <span class="attach-chip-icon">📎</span>
      <span class="attach-chip-name" title="${escHtml(name)}">${escHtml(displayName)}</span>
      ${state === "uploading"
        ? `<span class="attach-chip-remove" style="display:none">✕</span>`
        : `<span class="attach-chip-remove">✕</span>`}
    `;
    const removeBtn = chip.querySelector(".attach-chip-remove");
    removeBtn.addEventListener("click", () => {
      const idx = this._items.findIndex(i => i.chipEl === chip);
      if (idx !== -1) this._items.splice(idx, 1);
      chip.remove();
      if (!this._chips.children.length) this._chips.classList.remove("has-chips");
    });
    this._chips.appendChild(chip);
    this._chips.classList.add("has-chips");
    return chip;
  }

  _removeChip(item) {
    const idx = this._items.indexOf(item);
    if (idx !== -1) this._items.splice(idx, 1);
    item.chipEl.remove();
    if (!this._chips.children.length) this._chips.classList.remove("has-chips");
  }
}

// ── Job list ─────────────────────────────────────────────────────────────────

let _queuePositions = {};

async function loadJobList() {
  const [jobsRes, queueRes] = await Promise.all([
    fetch("/api/jobs"),
    fetch("/api/queue"),
  ]);
  const jobs  = await jobsRes.json();
  const queue = await queueRes.json();
  _queuePositions = {};
  for (const item of queue) _queuePositions[item.job_id] = item.position;
  renderJobList(jobs);
}

function renderJobList(jobs) {
  jobList.innerHTML = "";
  if (!jobs.length) {
    jobList.innerHTML = '<div class="no-jobs">No jobs yet.</div>';
    return;
  }
  for (const job of jobs) {
    const el = document.createElement("div");
    el.className = "job-item" + (job.id === activeJobId ? " active" : "");
    el.dataset.id = job.id;
    const label = job.title || (job.prompt.length > 55 ? job.prompt.slice(0, 52) + "…" : job.prompt);
    const ts = new Date(job.updated_at + "Z").toLocaleTimeString();
    const qpos = _queuePositions[job.id];
    const queueBadge = qpos ? `<span class="badge badge-queued queue-pos">#${qpos}</span>` : "";
    const unreadDot = (hasUnread(job) && job.id !== activeJobId) ? `<span class="unread-dot" title="New activity"></span>` : "";
    el.innerHTML = `
      <div class="job-item-prompt">${escHtml(label)}${unreadDot}</div>
      <div class="job-item-meta">
        ${queueBadge}
        <span class="badge badge-${job.state}">${formatState(job.state)}</span>
        <span class="job-item-time">${ts}</span>
      </div>`;
    el.addEventListener("click", () => openJob(job.id));
    jobList.appendChild(el);
  }
}

function formatState(s) {
  const map = { paused_due_to_limit: "limited", running: "running", completed: "done", failed: "failed", queued: "queued" };
  return map[s] || s;
}

// ── Open a job ────────────────────────────────────────────────────────────────

async function openJob(jobId) {
  if (activeJobId === jobId) return;
  closeSse();
  activeJobId = jobId;
  seqCount = 0;
  _renderedSeqs.clear();
  _seenSessionIds.clear();
  _pendingUserMsgs.clear();

  const res = await fetch(`/api/jobs/${jobId}`);
  activeJob = await res.json();

  renderDetailPanel(activeJob);
  outputWrap.innerHTML = "";
  outputWrap.classList.add("visible");
  emptyState.style.display = "none";
  setThinking(activeJob.state === "running", "Claude is working…");

  document.querySelectorAll(".job-item").forEach(el => {
    el.classList.toggle("active", el.dataset.id === jobId);
    if (el.dataset.id === jobId) {
      const dot = el.querySelector(".unread-dot");
      if (dot) dot.remove();
    }
  });

  openSse(jobId);
  if (activeJob) markJobRead(jobId, activeJob.last_seq || 0);
}

function renderDetailPanel(job) {
  detailEmpty.style.display = "none";
  detailContent.style.display = "block";

  // Title row
  const displayTitle = job.title || (job.prompt.length > 60 ? job.prompt.slice(0, 57) + "…" : job.prompt);
  detailTitle.textContent = displayTitle;
  editTitleInput.value = job.title || "";

  detailPrompt.textContent = job.prompt;
  metaState.innerHTML = `<span class="badge badge-${job.state}">${formatState(job.state)}</span>`;
  metaId.textContent  = job.id.slice(0, 8) + "…";
  metaId.title        = job.id;
  metaCreated.textContent = fmtTime(job.created_at);
  metaUpdated.textContent = fmtTime(job.updated_at);

  metaCwdWrap.style.display    = job.working_dir  ? "flex" : "none";
  metaCwd.textContent           = job.working_dir  || "";
  metaSessionWrap.style.display = job.session_id   ? "flex" : "none";
  metaSession.textContent       = job.session_id   ? job.session_id.slice(0, 12) + "…" : "";
  metaSession.title             = job.session_id   || "";
  metaResumesWrap.style.display = job.resume_count > 0 ? "flex" : "none";
  metaResumes.textContent       = job.resume_count;
  metaErrorWrap.style.display   = job.error        ? "flex" : "none";
  metaError.textContent         = job.error        || "";

  const hasTokens = (job.input_tokens || 0) + (job.output_tokens || 0) > 0;
  metaTokensWrap.style.display = hasTokens ? "flex" : "none";
  metaTokens.textContent = hasTokens
    ? `↑${fmtNum(job.input_tokens)} ↓${fmtNum(job.output_tokens)}`
    : "";

  const canCancel = job.state === "running" || job.state === "queued";
  cancelBtn.classList.toggle("visible", canCancel);
  cancelBtn.textContent = job.state === "queued" ? "✕ Remove from queue" : "✕ Cancel";

  updateMsgBar(job);
}

function updateMsgBar(job) {
  const canMsg = job && !!job.session_id;
  msgBar.classList.toggle("visible", !!canMsg);
  if (msgInput) {
    const finished = job && (job.state === "completed" || job.state === "failed");
    msgInput.placeholder = finished ? "Continue this session… (Enter to send)" : "Send a message to Claude… (Enter to send)";
  }
}

let _lastToolName = null;

function setThinking(active, label) {
  thinkingIndicator.classList.toggle("visible", !!active);
  if (active && label) thinkingLabel.textContent = label;
  else if (!active) thinkingLabel.textContent = "Claude is working…";
  // Scroll to show indicator when it appears
  if (active) thinkingIndicator.scrollIntoView({ block: "nearest" });
}

function fmtTime(iso) {
  return new Date(iso + "Z").toLocaleString();
}

function fmtNum(n) {
  if (!n) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

// ── Inline title edit ─────────────────────────────────────────────────────────

function _showTitleEdit() {
  detailTitle.style.display = "none";
  editTitleBtn.style.display = "none";
  editTitleInput.style.display = "";
  editTitleInput.focus();
  editTitleInput.select();
}

function _hideTitleEdit() {
  detailTitle.style.display = "";
  editTitleBtn.style.display = "";
  editTitleInput.style.display = "none";
}

async function _saveTitleEdit() {
  if (!activeJobId) { _hideTitleEdit(); return; }
  const newTitle = editTitleInput.value.trim() || null;
  _hideTitleEdit();
  const res = await fetch(`/api/jobs/${activeJobId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: newTitle }),
  });
  if (res.ok) {
    const job = await res.json();
    activeJob = job;
    detailTitle.textContent = job.title || job.prompt.slice(0, 60);
    loadJobList();
  }
}

editTitleBtn.addEventListener("click", _showTitleEdit);
editTitleInput.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); _saveTitleEdit(); }
  if (e.key === "Escape") _hideTitleEdit();
});
editTitleInput.addEventListener("blur", _saveTitleEdit);

// ── Stats polling ─────────────────────────────────────────────────────────────

async function refreshStats() {
  try {
    const res = await fetch("/api/stats");
    if (!res.ok) return;
    const s = await res.json();
    statSpawned.textContent = s.processes_spawned;
    statRunning.textContent = s.jobs_running;
    statInTok.textContent   = fmtNum(s.total_input_tokens);
    statOutTok.textContent  = fmtNum(s.total_output_tokens);
  } catch {}
}

refreshStats();
setInterval(refreshStats, 5_000);

// ── SSE streaming ─────────────────────────────────────────────────────────────

let _sseReconnectTimer = null;
let _streamDone = false;  // true once server sent stream_end for this job

function openSse(jobId) {
  _streamDone = false;
  const url = `/api/jobs/${jobId}/stream?after=${seqCount}`;
  evtSource = new EventSource(url);

  evtSource.onopen = () => {
    statusDot.className = "connected";
    statusText.textContent = "Connected";
    if (_sseReconnectTimer) { clearTimeout(_sseReconnectTimer); _sseReconnectTimer = null; }
  };

  evtSource.onmessage = e => {
    try { handleEvent(JSON.parse(e.data)); } catch {}
  };

  evtSource.onerror = () => {
    evtSource.close();
    evtSource = null;

    // If stream_end was already received, or job is terminal: show Finished, no reconnect
    if (_streamDone || (activeJob && isTerminal(activeJob.state))) {
      statusDot.className = "";
      statusText.textContent = "Finished";
      return;
    }

    statusDot.className = "reconnecting";
    statusText.textContent = "Reconnecting…";
    _sseReconnectTimer = setTimeout(() => {
      if (activeJobId === jobId) openSse(jobId);
    }, 2_000);
  };
}

function closeSse() {
  if (_sseReconnectTimer) { clearTimeout(_sseReconnectTimer); _sseReconnectTimer = null; }
  if (evtSource) { evtSource.close(); evtSource = null; }
  statusDot.className = "";
  statusText.textContent = "Disconnected";
}

function isTerminal(state) {
  return state === "completed" || state === "failed";
}

// ── Event handling ────────────────────────────────────────────────────────────

function handleEvent(data) {
  const type = data.type || data.event_type || "raw";
  if (type === "ping") return;

  // Server signals stream ended cleanly — no reconnect needed
  if (type === "stream_end") {
    _streamDone = true;
    setThinking(false);
    statusDot.className = "";
    statusText.textContent = "Finished";
    if (evtSource) { evtSource.close(); evtSource = null; }
    if (_sseReconnectTimer) { clearTimeout(_sseReconnectTimer); _sseReconnectTimer = null; }
    // Refresh job state in case we missed the final orch_state event
    if (activeJobId) {
      fetch(`/api/jobs/${activeJobId}`).then(r => r.json()).then(j => {
        activeJob = j;
        renderDetailPanel(j);
      });
    }
    return;
  }

  // Deduplicate by seq
  if (data.seq != null) {
    if (_renderedSeqs.has(data.seq)) return;
    _renderedSeqs.add(data.seq);
    seqCount = Math.max(seqCount, data.seq);
    markJobRead(jobId, data.seq);
  }
  statusSeq.textContent = `${seqCount} events`;

  if (type === "orch_state") {
    const state = data.state;
    if (activeJob) activeJob.state = state;
    metaState.innerHTML = `<span class="badge badge-${state}">${formatState(state)}</span>`;
    const canCancel = state === "running" || state === "queued";
    cancelBtn.classList.toggle("visible", canCancel);
    cancelBtn.textContent = state === "queued" ? "✕ Remove from queue" : "✕ Cancel";
    if (state === "running") {
      statusText.textContent = "Claude is working…";
      statusDot.className = "connected";
      setThinking(true, "Claude is working…");
    } else {
      setThinking(false);
      statusText.textContent = isTerminal(state) ? "Finished" : formatState(state);
    }
    loadJobList();
    if (activeJobId) fetch(`/api/jobs/${activeJobId}`).then(r => r.json()).then(j => {
      activeJob = j;
      renderDetailPanel(j);
    });
  }

  if (type === "orch_queue") {
    _queuePositions = {};
    for (const item of (data.queue || [])) _queuePositions[item.job_id] = item.position;
    loadJobList();
  }

  // Update thinking label when tool activity is seen
  if (type === "tool_use" && activeJob && activeJob.state === "running") {
    const name = data.name || (data.message?.content || []).find(p => p.type === "tool_use")?.name;
    if (name) setThinking(true, `Running ${name}…`);
  } else if ((type === "assistant" || type === "result") && activeJob && activeJob.state === "running") {
    setThinking(true, "Claude is working…");
  }

  const node = renderEvent(data, type);
  if (node) { outputWrap.appendChild(node); scrollToBottom(); }
}

// ── User message text renderer ────────────────────────────────────────────────

const _IMG_EXTS = /\.(png|jpe?g|gif|webp|svg|bmp)$/i;

function _renderUserMsgText(text) {
  // Split on the "Attached files:" block if present
  const attachedIdx = text.indexOf("\n\nAttached files:\n");
  const mainText = attachedIdx === -1 ? text : text.slice(0, attachedIdx);
  const attachedBlock = attachedIdx === -1 ? "" : text.slice(attachedIdx + "\n\nAttached files:\n".length);

  let html = `<span class="user-msg-text">${escHtml(mainText)}</span>`;

  if (attachedBlock) {
    const paths = attachedBlock.split("\n").map(l => l.replace(/^- /, "").trim()).filter(Boolean);
    const imgs = paths.filter(p => _IMG_EXTS.test(p));
    const others = paths.filter(p => !_IMG_EXTS.test(p));

    if (imgs.length) {
      html += `<div class="user-msg-images">` +
        imgs.map(p => {
          const url = `/api/uploads/file?path=${encodeURIComponent(p)}`;
          const name = p.split("/").pop();
          return `<a href="${url}" target="_blank" title="${escHtml(name)}" class="user-msg-img-link">` +
                 `<img src="${url}" alt="${escHtml(name)}" class="user-msg-img"></a>`;
        }).join("") +
        `</div>`;
    }
    if (others.length) {
      html += `<div class="user-msg-attachments">` +
        others.map(p => `<span class="attach-chip">📎 <span class="attach-chip-name">${escHtml(p.split("/").pop())}</span></span>`).join("") +
        `</div>`;
    }
  }

  return html;
}

// ── Event renderer ────────────────────────────────────────────────────────────

function renderEvent(data, outerType) {
  // Two stream shapes:
  //   LIVE:   {seq, type, ts, raw, ...fields}   from runner broadcast
  //   REPLAY: {seq, event_type, raw, ts}         from store.get_logs()
  let parsed = data;
  const rawLine = data.raw || "";

  if (data.event_type && !data.type) {
    // Replay row — re-parse the raw JSON to get full event data
    try { parsed = { ...JSON.parse(rawLine), seq: data.seq, ts: data.ts }; }
    catch { parsed = { type: data.event_type, raw: rawLine, seq: data.seq, ts: data.ts }; }
  } else if (rawLine && rawLine.startsWith("{")) {
    // Live event — merge raw for any fields missing from sanitised broadcast
    try {
      const fromRaw = JSON.parse(rawLine);
      parsed = { ...fromRaw, ...data };  // data wins (has seq/ts/type already set)
    } catch {}
  }

  const evType = parsed.type || outerType;
  const ts = data.ts || parsed.ts || null;

  switch (evType) {

    case "assistant": {
      const text = extractAssistantText(parsed);
      if (!text || !text.trim()) return null;
      const el = block("assistant", ts);
      el._content.innerHTML = renderMarkdown(text);
      return el;
    }

    case "tool_use": {
      const uses = extractToolUses(parsed);
      if (!uses.length) return null;
      const frag = document.createDocumentFragment();
      for (const { name, input } of uses) {
        const el = block("tool_use", ts, "tool-block");
        const inputStr = formatToolInput(name, input);
        el._content.innerHTML =
          `<span class="tool-name">❯ ${escHtml(name)}</span>` +
          (inputStr ? `<span class="tool-input">${escHtml(inputStr)}</span>` : "");
        frag.appendChild(el);
      }
      return frag;
    }

    case "tool_result": {
      const text = extractToolResultText(parsed);
      if (!text || !text.trim()) return null;
      const el = block("tool_result", ts, "tool-result-block");
      el._content.innerHTML = `<span class="tool-result-label">◀ result</span><span class="tool-result-text">${escHtml(truncate(text, 600))}</span>`;
      return el;
    }

    case "user":
      return null;

    case "user_msg": {
      const text = parsed.text || "";
      if (_pendingUserMsgs.has(text)) { _pendingUserMsgs.delete(text); return null; }
      const el = block("user_msg", ts);
      const queuedBadge = parsed.queued ? `<span class="user-msg-queued">queued</span>` : "";
      const renderedText = _renderUserMsgText(text);
      el._content.innerHTML = `<span class="user-msg-label">You</span>${queuedBadge}${renderedText}`;
      return el;
    }

    case "result": {
      const subtype = parsed.subtype || "";
      const ok = subtype === "success";
      const el = block("result", ts, "result-block");
      const errMsg = extractErrorMessage(parsed);
      el._content.innerHTML =
        `<span class="result-icon ${ok ? "result-ok" : "result-err"}">${ok ? "✓ Completed" : "✗ " + escHtml(subtype || "error")}</span>` +
        (errMsg ? `<span class="result-error-msg">${escHtml(errMsg)}</span>` : "");
      return el;
    }

    case "orch_status": {
      const el = block("orch_status", ts, "orch-block");
      el._content.innerHTML = `<span class="orch-label">⏳</span> ${escHtml(parsed.message || "")}`;
      return el;
    }

    case "orch_state": {
      const el = block("orch_state", ts, "orch-block");
      el._content.innerHTML = `<span class="orch-label">◈</span> <span class="badge badge-${parsed.state}">${formatState(parsed.state)}</span>`;
      return el;
    }

    case "system":
    case "init": {
      const sid = parsed.session_id || parsed.sessionId;
      if (!sid) return null;
      if (_seenSessionIds.has(sid)) return null;
      // Suppress resume system events — same session continuing, not a new session
      if (activeJob && activeJob.session_id && sid !== activeJob.session_id) return null;
      _seenSessionIds.add(sid);
      const el = block("system", ts);
      el._content.textContent = `⬡ session ${sid}`;
      return el;
    }

    case "raw": {
      const raw = (parsed.raw || rawLine || "").trim();
      if (!raw || raw.startsWith("{") || raw.startsWith("[")) return null;
      const el = block("raw", ts);
      el._content.textContent = raw;
      return el;
    }

    default:
      return null;
  }
}

// ── Extraction helpers ────────────────────────────────────────────────────────

function extractAssistantText(ev) {
  if (ev.message?.content) {
    const parts = Array.isArray(ev.message.content) ? ev.message.content : [];
    const text = parts.filter(p => p.type === "text").map(p => p.text).join("");
    if (text) return text;
  }
  if (typeof ev.text === "string") return ev.text;
  return "";
}

function extractToolUses(ev) {
  if (ev.type === "tool_use" && ev.name) return [{ name: ev.name, input: ev.input || {} }];
  if (ev.message?.content) {
    return (Array.isArray(ev.message.content) ? ev.message.content : [])
      .filter(p => p.type === "tool_use" && p.name)
      .map(p => ({ name: p.name, input: p.input || {} }));
  }
  return [];
}

function extractToolResultText(ev) {
  if (typeof ev.content === "string") return ev.content;
  if (Array.isArray(ev.content)) return ev.content.filter(p => p.type === "text").map(p => p.text).join("\n");
  return "";
}

function extractErrorMessage(ev) {
  if (!ev.error) return "";
  if (typeof ev.error === "string") return ev.error;
  if (typeof ev.error === "object") return ev.error.message || JSON.stringify(ev.error);
  return "";
}

function formatToolInput(name, input) {
  if (!input || typeof input !== "object") return "";
  const val = input.command ?? input.file_path ?? input.path ?? input.content ?? input.query ?? input.url ?? null;
  if (val !== null) return truncate(String(val), 200);
  const keys = Object.keys(input);
  if (keys.length) return truncate(`${keys[0]}: ${JSON.stringify(input[keys[0]])}`, 200);
  return "";
}

// ── Markdown renderer ─────────────────────────────────────────────────────────

function renderMarkdown(text) {
  let s = escHtml(text);
  s = s.replace(/```(?:\w+)?\n([\s\S]*?)```/g, (_, c) => `<pre class="code-block">${c}</pre>`);
  s = s.replace(/`([^`\n]+)`/g, (_, c) => `<code class="inline-code">${c}</code>`);
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, "<em>$1</em>");
  s = s.replace(/^(#{1,3})\s+(.+)$/gm, (_, h, c) => `<h${Math.min(h.length + 2, 6)} class="md-heading">${c}</h${Math.min(h.length + 2, 6)}>`);
  s = s.replace(/^[ \t]*[-*]\s+(.+)$/gm, '<li class="md-li">$1</li>');
  s = s.replace(/(<li[\s\S]*?<\/li>)/g, '<ul class="md-ul">$1</ul>');
  s = s.replace(/\n/g, "<br>");
  return s;
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function block(type, ts, ...extra) {
  const el = document.createElement("div");
  el.className = ["msg-block", `ev-${type}`, ...extra].join(" ");

  // Timestamp on the left, always visible — full datetime in tooltip
  const stamp = document.createElement("span");
  stamp.className = "msg-ts";
  stamp.textContent = ts ? fmtTs(ts) : "";
  if (ts) stamp.dataset.fullts = fmtTsFull(ts);
  el.appendChild(stamp);

  // Content wrapper
  const content = document.createElement("div");
  content.className = "msg-content";
  el.appendChild(content);
  // Caller sets innerHTML/textContent on el — we intercept that below
  // by storing a ref so renderEvent can fill content directly
  el._content = content;
  return el;
}

function _tsToDate(ts) {
  if (!ts) return null;
  let s = String(ts);
  // SQLite datetime('now') returns "YYYY-MM-DD HH:MM:SS" (space, no Z) — normalise to ISO UTC
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) s = s.replace(" ", "T") + "Z";
  else if (s.includes("T") && !s.endsWith("Z")) s += "Z";
  const d = new Date(s);
  return isNaN(d) ? null : d;
}

function fmtTs(ts) {
  const d = _tsToDate(ts);
  return d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}

function fmtTsFull(ts) {
  const d = _tsToDate(ts);
  return d ? d.toLocaleString([], {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }) : "";
}

function truncate(s, n) { return s.length > n ? s.slice(0, n) + "…" : s; }

function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function scrollToBottom() { outputWrap.scrollTop = outputWrap.scrollHeight; }

// ── New job modal ─────────────────────────────────────────────────────────────

document.getElementById("new-job-btn").addEventListener("click", () => openModal());

function openModal(prefill) {
  modalOverlay.classList.add("open");
  if (prefill) {
    promptInput.value = prefill.prompt || "";
    cwdInput.value = prefill.working_dir || "";
    if (titleInput) titleInput.value = prefill.title || "";
  }
  promptInput.focus();
}

function closeModal() {
  modalOverlay.classList.remove("open");
  promptInput.value = "";
  cwdInput.value = "";
  if (titleInput) titleInput.value = "";
  if (typeof modalAttachments !== "undefined") modalAttachments.clear();
}

document.getElementById("modal-cancel").addEventListener("click", closeModal);
modalOverlay.addEventListener("click", e => { if (e.target === modalOverlay) closeModal(); });
document.getElementById("modal-submit").addEventListener("click", submitJob);
promptInput.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitJob(); });

async function submitJob() {
  const rawPrompt = promptInput.value.trim();
  if (!rawPrompt) { promptInput.focus(); return; }
  const working_dir = cwdInput.value.trim() || null;
  const title = titleInput ? titleInput.value.trim() || null : null;
  const paths = (typeof modalAttachments !== "undefined") ? modalAttachments.consumePaths() : [];
  const prompt = paths.length
    ? rawPrompt + "\n\nAttached files:\n" + paths.map(p => "- " + p).join("\n")
    : rawPrompt;
  closeModal();
  const res = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, working_dir, title }),
  });
  const job = await res.json();
  await loadJobList();
  openJob(job.id);
}

// ── Message bar ───────────────────────────────────────────────────────────────

async function sendMessage() {
  const rawText = msgInput.value.trim();
  const paths = msgAttachments.consumePaths();
  const text = paths.length
    ? rawText + "\n\nAttached files:\n" + paths.map(p => "- " + p).join("\n")
    : rawText;
  if (!text || !activeJobId) return;
  msgSend.disabled = true;
  msgInput.disabled = true;

  // Render user bubble immediately
  _pendingUserMsgs.add(text);
  const isPaused = activeJob && activeJob.state === "paused_due_to_limit";
  const el = block("user_msg", new Date().toISOString());
  const qBadge = isPaused ? `<span class="user-msg-queued">queued</span>` : "";
  el._content.innerHTML = `<span class="user-msg-label">You</span>${qBadge}<span class="user-msg-text">${escHtml(text)}</span>`;
  outputWrap.appendChild(el);
  scrollToBottom();

  const jobId = activeJobId;

  try {
    // POST the message first — server sets job state back to running
    const res = await fetch(`/api/jobs/${jobId}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const errEl = block("raw");
      errEl.textContent = "✗ " + (err.detail || "Failed to send message");
      outputWrap.appendChild(errEl);
      scrollToBottom();
    } else {
      // Server has set state=running — show indicator immediately, then open SSE
      setThinking(true, "Claude is working…");
      if (!evtSource && activeJobId === jobId) {
        _streamDone = false;
        openSse(jobId);
      }
    }
  } finally {
    msgInput.value = "";
    msgSend.disabled = false;
    msgInput.disabled = false;
    msgInput.focus();
  }
}

msgSend.addEventListener("click", sendMessage);
msgInput.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); } });

// ── Cancel button ─────────────────────────────────────────────────────────────

cancelBtn.addEventListener("click", async () => {
  if (!activeJobId || !confirm("Cancel this job?")) return;
  await fetch(`/api/jobs/${activeJobId}`, { method: "DELETE" });
  loadJobList();
  fetch(`/api/jobs/${activeJobId}`).then(r => r.json()).then(j => { activeJob = j; renderDetailPanel(j); });
});

// ── Retry button ──────────────────────────────────────────────────────────────

document.getElementById("retry-btn").addEventListener("click", () => {
  if (activeJob) openModal({ prompt: activeJob.prompt, working_dir: activeJob.working_dir });
});

// ── Auto-refresh ──────────────────────────────────────────────────────────────

setInterval(async () => {
  const res  = await fetch("/api/jobs");
  const jobs = await res.json();
  renderJobList(jobs);
  if (activeJobId) {
    const job = jobs.find(j => j.id === activeJobId);
    if (job) { activeJob = job; metaUpdated.textContent = fmtTime(job.updated_at); }
  }
}, 5_000);

// ── Init ──────────────────────────────────────────────────────────────────────

const modalAttachments = new Attachments(
  document.getElementById("modal-chips"),
  document.getElementById("prompt-input"),
  document.getElementById("modal-file-input")
);

const msgAttachments = new Attachments(
  document.getElementById("msg-chips"),
  document.getElementById("msg-input"),
  document.getElementById("msg-file-input")
);

document.getElementById("modal-attach").addEventListener("click", () => modalAttachments.openPicker());
document.getElementById("msg-attach").addEventListener("click", () => msgAttachments.openPicker());

loadJobList();

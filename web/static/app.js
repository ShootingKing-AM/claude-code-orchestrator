"use strict";

// ── State ────────────────────────────────────────────────────────────────────

let activeJobId = null;
let activeJob   = null;
let evtSource   = null;
let seqCount    = 0;
const _renderedSeqs   = new Set();  // dedup SSE events by seq on reconnect
const _seenSessionIds = new Set();  // show each session ID only once

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

// ── Job list ─────────────────────────────────────────────────────────────────

let _queuePositions = {};  // job_id → position

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
    jobList.innerHTML = '<div style="padding:16px;color:var(--text-muted);font-size:12px;">No jobs yet.</div>';
    return;
  }
  for (const job of jobs) {
    const el = document.createElement("div");
    el.className = "job-item" + (job.id === activeJobId ? " active" : "");
    el.dataset.id = job.id;
    const prompt = job.prompt.length > 60 ? job.prompt.slice(0, 57) + "…" : job.prompt;
    const ts = new Date(job.updated_at + "Z").toLocaleTimeString();
    const qpos = _queuePositions[job.id];
    const queueBadge = qpos
      ? `<span class="badge badge-queued queue-pos">#${qpos} in queue</span>`
      : "";
    el.innerHTML = `
      <div class="job-item-prompt">${escHtml(prompt)}</div>
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
  return s === "paused_due_to_limit" ? "rate limited" : s;
}

// ── Open a job ────────────────────────────────────────────────────────────────

async function openJob(jobId) {
  if (activeJobId === jobId) return;
  closeSse();
  activeJobId = jobId;
  seqCount = 0;
  _renderedSeqs.clear();   // reset dedup set for new job
  _seenSessionIds.clear(); // reset session dedup for new job

  const res = await fetch(`/api/jobs/${jobId}`);
  const job = await res.json();
  activeJob = job;

  renderDetailPanel(job);
  updateMsgBar(job);
  outputWrap.innerHTML = "";
  outputWrap.classList.add("visible");
  emptyState.style.display = "none";

  document.querySelectorAll(".job-item").forEach(el =>
    el.classList.toggle("active", el.dataset.id === jobId));

  openSse(jobId);
}

function renderDetailPanel(job) {
  detailEmpty.style.display = "none";
  detailContent.style.display = "block";

  detailPrompt.textContent = job.prompt;
  metaState.innerHTML = `<span class="badge badge-${job.state}">${formatState(job.state)}</span>`;
  metaId.textContent  = job.id.slice(0, 8) + "…";
  metaId.title        = job.id;
  metaCreated.textContent = fmtTime(job.created_at);
  metaUpdated.textContent = fmtTime(job.updated_at);

  metaCwdWrap.style.display     = job.working_dir  ? "flex" : "none";
  metaCwd.textContent            = job.working_dir  || "";
  metaSessionWrap.style.display  = job.session_id   ? "flex" : "none";
  metaSession.textContent        = job.session_id   ? job.session_id.slice(0, 12) + "…" : "";
  metaSession.title              = job.session_id   || "";
  metaResumesWrap.style.display  = job.resume_count > 0 ? "flex" : "none";
  metaResumes.textContent        = job.resume_count;
  metaErrorWrap.style.display    = job.error        ? "flex" : "none";
  metaError.textContent          = job.error        || "";

  const canCancel = job.state === "running" || job.state === "queued";
  cancelBtn.classList.toggle("visible", canCancel);
  cancelBtn.textContent = job.state === "queued" ? "✕ Remove from queue" : "✕ Cancel";
}

function fmtTime(iso) {
  return new Date(iso + "Z").toLocaleString();
}

// ── SSE streaming ─────────────────────────────────────────────────────────────

let _sseReconnectTimer = null;

function openSse(jobId) {
  // Pass after= so server only sends events we haven't seen yet
  const url = `/api/jobs/${jobId}/stream?after=${seqCount}`;
  evtSource = new EventSource(url);

  evtSource.onopen = () => {
    statusDot.className = "connected";
    statusText.textContent = "Connected";
    if (_sseReconnectTimer) { clearTimeout(_sseReconnectTimer); _sseReconnectTimer = null; }
  };

  evtSource.onmessage = e => handleEvent(JSON.parse(e.data));

  evtSource.onerror = () => {
    statusDot.className = "reconnecting";
    statusText.textContent = "Reconnecting…";
    // Close and manually reconnect after a delay, passing current seqCount
    // so we don't replay already-seen events. EventSource auto-reconnects
    // from the start so we manage reconnection ourselves.
    evtSource.close();
    evtSource = null;
    _sseReconnectTimer = setTimeout(() => {
      if (activeJobId === jobId) openSse(jobId);
    }, 3000);
  };
}

function closeSse() {
  if (_sseReconnectTimer) { clearTimeout(_sseReconnectTimer); _sseReconnectTimer = null; }
  if (evtSource) { evtSource.close(); evtSource = null; }
  statusDot.className = "";
  statusText.textContent = "Disconnected";
}

// ── Event handling ────────────────────────────────────────────────────────────

// (both Sets declared at top of file)

function handleEvent(data) {
  const type = data.type || "raw";
  if (type === "ping") return;

  // Deduplicate — skip events we've already rendered
  if (data.seq) {
    if (_renderedSeqs.has(data.seq)) return;
    _renderedSeqs.add(data.seq);
    seqCount = Math.max(seqCount, data.seq);
  }
  statusSeq.textContent = `${seqCount} events`;

  if (type === "orch_state") {
    const state = data.state;
    metaState.innerHTML = `<span class="badge badge-${state}">${formatState(state)}</span>`;
    const canCancel = state === "running" || state === "queued";
    cancelBtn.classList.toggle("visible", canCancel);
    cancelBtn.textContent = state === "queued" ? "✕ Remove from queue" : "✕ Cancel";
    loadJobList();
    if (activeJobId) fetch(`/api/jobs/${activeJobId}`).then(r => r.json()).then(j => { activeJob = j; renderDetailPanel(j); updateMsgBar(j); });
  }

  if (type === "orch_queue") {
    // Update queue positions and refresh sidebar
    _queuePositions = {};
    for (const item of (data.queue || [])) _queuePositions[item.job_id] = item.position;
    loadJobList();
  }

  const node = renderEvent(data, type);
  if (node) { outputWrap.appendChild(node); scrollToBottom(); }
}

// ── Event renderer ────────────────────────────────────────────────────────────
// Returns a DOM node or null (null = silently skip this event).

function renderEvent(data, type) {

  // The SSE stream has two shapes:
  //   LIVE:    {seq, type, raw, ...parsed_fields}  — from runner broadcast
  //   REPLAY:  {seq, event_type, raw, ts}          — from store.get_logs()
  // Normalise to a single `parsed` object with a reliable `type` field.
  let parsed = data;
  const rawLine = data.raw || "";

  // Replay shape: event_type instead of type, raw is the original JSON line
  if (data.event_type && !data.type) {
    try { parsed = { ...JSON.parse(rawLine), seq: data.seq }; } catch {
      parsed = { type: data.event_type, raw: rawLine, seq: data.seq };
    }
  }

  // If raw is a JSON string with more info than what's already in parsed, merge it
  if (rawLine && rawLine.startsWith("{") && !parsed.message && !parsed.content) {
    try {
      const fromRaw = JSON.parse(rawLine);
      parsed = { ...fromRaw, seq: data.seq };
    } catch {}
  }

  const evType = parsed.type || type;

  switch (evType) {

    // ── Assistant text output ───────────────────────────────────────
    case "assistant": {
      const text = extractAssistantText(parsed);
      if (!text || !text.trim()) return null;
      const el = makeBlock("assistant");
      el.innerHTML = renderMarkdown(text);
      return el;
    }

    // ── Tool calls ──────────────────────────────────────────────────
    case "tool_use": {
      // May appear at top level OR nested inside an assistant message content array
      const uses = extractToolUses(parsed);
      if (!uses.length) return null;
      const wrap = document.createDocumentFragment();
      for (const { name, input } of uses) {
        const el = makeBlock("tool_use", "tool-block");
        const inputStr = formatToolInput(name, input);
        el.innerHTML =
          `<span class="tool-name">❯ ${escHtml(name)}</span>` +
          (inputStr ? `<span class="tool-input">${escHtml(inputStr)}</span>` : "");
        wrap.appendChild(el);
      }
      return wrap;
    }

    // ── Tool results ────────────────────────────────────────────────
    case "tool_result": {
      const text = extractToolResultText(parsed);
      if (!text || !text.trim()) return null;
      const el = makeBlock("tool_result", "tool-result-block");
      el.innerHTML = `<span class="tool-result-label">◀ result</span><span class="tool-result-text">${escHtml(truncate(text, 500))}</span>`;
      return el;
    }

    // ── User echo (--verbose re-emits user messages) ─────────────────
    // Skip these — they're just our own prompt echoed back.
    case "user":
      return null;

    // ── Final result ─────────────────────────────────────────────────
    case "result": {
      const subtype = parsed.subtype || "";
      const ok = subtype === "success";
      const el = makeBlock("result", "result-block");
      const errorMsg = extractErrorMessage(parsed);
      el.innerHTML =
        `<span class="result-icon ${ok ? "result-ok" : "result-err"}">${ok ? "✓ Completed" : "✗ " + escHtml(subtype || "error")}</span>` +
        (errorMsg ? `<span class="result-error-msg">${escHtml(errorMsg)}</span>` : "");
      return el;
    }

    // ── Orchestrator status (rate-limit countdown etc.) ──────────────
    case "orch_status": {
      const el = makeBlock("orch_status", "orch-block");
      el.innerHTML = `<span class="orch-label">⏳</span> ${escHtml(parsed.message || "")}`;
      return el;
    }

    // ── Orchestrator state transition ────────────────────────────────
    case "orch_state": {
      const el = makeBlock("orch_state", "orch-block");
      el.innerHTML = `<span class="orch-label">◈ state →</span> <span class="badge badge-${parsed.state}">${formatState(parsed.state)}</span>`;
      return el;
    }

    // ── System / init: show session ID once only ─────────────────────
    case "system":
    case "init": {
      const sid = parsed.session_id || parsed.sessionId;
      if (!sid) return null;
      if (_seenSessionIds.has(sid)) return null;   // suppress duplicates
      _seenSessionIds.add(sid);
      const el = makeBlock("system");
      el.textContent = `⬡ session ${sid}`;
      return el;
    }

    // ── Raw non-JSON lines (e.g. plain-text error from claude) ───────
    case "raw": {
      const raw = (parsed.raw || rawLine || "").trim();
      if (!raw) return null;
      // JSON lines are re-parsed above and handled by their own type case
      if (raw.startsWith("{") || raw.startsWith("[")) return null;
      const el = makeBlock("raw");
      el.textContent = raw;
      return el;
    }

    // ── Everything else: silently drop ───────────────────────────────
    default:
      return null;
  }
}

// ── Extraction helpers ────────────────────────────────────────────────────────

function extractAssistantText(ev) {
  // Shape 1: ev.message.content = [{type:"text", text:"…"}]
  if (ev.message?.content) {
    const parts = Array.isArray(ev.message.content) ? ev.message.content : [];
    const text = parts.filter(p => p.type === "text").map(p => p.text).join("");
    if (text) return text;
  }
  // Shape 2: direct text field
  if (typeof ev.text === "string") return ev.text;
  return "";
}

function extractToolUses(ev) {
  const uses = [];
  // Top-level tool_use
  if (ev.type === "tool_use" && ev.name) {
    uses.push({ name: ev.name, input: ev.input || {} });
    return uses;
  }
  // Nested inside assistant message content
  if (ev.message?.content) {
    for (const part of (Array.isArray(ev.message.content) ? ev.message.content : [])) {
      if (part.type === "tool_use" && part.name) {
        uses.push({ name: part.name, input: part.input || {} });
      }
    }
  }
  return uses;
}

function extractToolResultText(ev) {
  if (typeof ev.content === "string") return ev.content;
  if (Array.isArray(ev.content)) {
    return ev.content.filter(p => p.type === "text").map(p => p.text).join("\n");
  }
  // Sometimes nested in message
  if (ev.message?.content) {
    const parts = Array.isArray(ev.message.content) ? ev.message.content : [];
    const res = parts.find(p => p.type === "tool_result");
    if (res) return extractToolResultText(res);
  }
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
  // Show the most meaningful single field per tool type
  const val = input.command ?? input.file_path ?? input.path ??
              input.content ?? input.query ?? input.url ?? null;
  if (val !== null) return truncate(String(val), 300);
  // Fallback: first key
  const keys = Object.keys(input);
  if (keys.length) return truncate(`${keys[0]}: ${JSON.stringify(input[keys[0]])}`, 300);
  return "";
}

// ── Markdown-lite renderer ────────────────────────────────────────────────────

function renderMarkdown(text) {
  // Escape HTML first
  let s = escHtml(text);

  // Fenced code blocks  ```lang\n...\n```
  s = s.replace(/```(?:\w+)?\n([\s\S]*?)```/g,
    (_, code) => `<pre class="code-block">${code}</pre>`);

  // Inline code  `...`
  s = s.replace(/`([^`\n]+)`/g,
    (_, c) => `<code class="inline-code">${c}</code>`);

  // Bold  **...**
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

  // Italic  *...*  (not inside bold)
  s = s.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, "<em>$1</em>");

  // Headers  ## Heading
  s = s.replace(/^(#{1,3})\s+(.+)$/gm, (_, hashes, content) => {
    const level = Math.min(hashes.length + 2, 6);  // h3–h5 in our context
    return `<h${level} class="md-heading">${content}</h${level}>`;
  });

  // Bullet lists  - item  or  * item
  s = s.replace(/^[ \t]*[-*]\s+(.+)$/gm, '<li class="md-li">$1</li>');
  s = s.replace(/(<li[\s\S]*?<\/li>)/g, '<ul class="md-ul">$1</ul>');

  // Newlines → <br> (but not inside pre blocks)
  s = s.replace(/\n/g, "<br>");

  return s;
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function makeBlock(type, ...extraClasses) {
  const el = document.createElement("div");
  el.className = ["msg-block", `ev-${type}`, ...extraClasses].join(" ");
  return el;
}

function truncate(s, n) { return s.length > n ? s.slice(0, n) + "…" : s; }

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function scrollToBottom() { outputWrap.scrollTop = outputWrap.scrollHeight; }

// ── New job modal ─────────────────────────────────────────────────────────────

document.getElementById("new-job-btn").addEventListener("click", () => openModal());

function openModal(prefill) {
  modalOverlay.classList.add("open");
  if (prefill) { promptInput.value = prefill.prompt || ""; cwdInput.value = prefill.working_dir || ""; }
  promptInput.focus();
}

function closeModal() {
  modalOverlay.classList.remove("open");
  promptInput.value = "";
  cwdInput.value = "";
}

document.getElementById("modal-cancel").addEventListener("click", closeModal);
modalOverlay.addEventListener("click", e => { if (e.target === modalOverlay) closeModal(); });
document.getElementById("modal-submit").addEventListener("click", submitJob);
promptInput.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitJob(); });

async function submitJob() {
  const prompt = promptInput.value.trim();
  if (!prompt) { promptInput.focus(); return; }
  const working_dir = cwdInput.value.trim() || null;
  closeModal();
  const res = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, working_dir }),
  });
  const job = await res.json();
  await loadJobList();
  openJob(job.id);
}

// ── Message bar ───────────────────────────────────────────────────────────────

const msgBar   = document.getElementById("msg-bar");
const msgInput = document.getElementById("msg-input");
const msgSend  = document.getElementById("msg-send");

function updateMsgBar(job) {
  const canMsg = job && job.session_id &&
    ["running", "completed", "paused_due_to_limit"].includes(job.state);
  msgBar.classList.toggle("visible", !!canMsg);
}

async function sendMessage() {
  const text = msgInput.value.trim();
  if (!text || !activeJobId) return;
  msgSend.disabled = true;
  msgInput.disabled = true;

  // Show the user message in the output immediately
  const el = makeBlock("user-msg");
  el.innerHTML = `<span class="user-msg-label">You</span>${escHtml(text)}`;
  outputWrap.appendChild(el);
  scrollToBottom();

  try {
    const res = await fetch(`/api/jobs/${activeJobId}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!res.ok) {
      const err = await res.json();
      const errEl = makeBlock("raw");
      errEl.textContent = "Error: " + (err.detail || "Failed to send");
      outputWrap.appendChild(errEl);
      scrollToBottom();
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
  if (activeJobId) fetch(`/api/jobs/${activeJobId}`).then(r => r.json()).then(j => renderDetailPanel(j));
});

// ── Retry button ──────────────────────────────────────────────────────────────

document.getElementById("retry-btn").addEventListener("click", () => {
  if (activeJob) openModal({ prompt: activeJob.prompt, working_dir: activeJob.working_dir });
});

// ── Auto-refresh job list ─────────────────────────────────────────────────────

setInterval(async () => {
  const res  = await fetch("/api/jobs");
  const jobs = await res.json();
  renderJobList(jobs);
  if (activeJobId) {
    const job = jobs.find(j => j.id === activeJobId);
    if (job) { activeJob = job; metaUpdated.textContent = fmtTime(job.updated_at); }
  }
}, 5000);

// ── Init ──────────────────────────────────────────────────────────────────────
loadJobList();

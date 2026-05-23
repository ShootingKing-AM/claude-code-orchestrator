# UX Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four UX features: editable job titles, a live stats panel, per-job token tracking, and a collapsible sidebar.

**Architecture:** Job model gains `title`, `input_tokens`, `output_tokens` fields (backward-compatible via `d.get()`). A new `GET /api/stats` endpoint aggregates counters from the scheduler and store. A new `PATCH /api/jobs/{id}` endpoint handles title edits. All frontend changes are in the existing vanilla JS files — no new files needed.

**Tech Stack:** Python 3.10 / FastAPI / SQLite (backend), vanilla JS / CSS (frontend), pytest (tests)

---

## File Map

| File | Action | What changes |
|---|---|---|
| `orchestrator/job.py` | Modify | Add `title`, `input_tokens`, `output_tokens` fields |
| `orchestrator/runner.py` | Modify | Extract `usage` from `result` events; accumulate on job |
| `orchestrator/scheduler.py` | Modify | Add `processes_spawned` counter; expose via `stats()` method |
| `web/server.py` | Modify | Add `PATCH /api/jobs/{id}` and `GET /api/stats` endpoints |
| `web/static/index.html` | Modify | Add title input to modal; stats bar in sidebar; collapse toggle button |
| `web/static/app.js` | Modify | Inline title editing; stats polling; sidebar collapse logic |
| `web/static/style.css` | Modify | Stats bar styles; collapsed sidebar styles; inline edit styles |
| `tests/test_job.py` | Modify | Add tests for new Job fields |
| `tests/test_stats.py` | Create | Tests for stats endpoint and token accumulation |

---

## Task 1: Job model — title, input_tokens, output_tokens fields

**Files:**
- Modify: `orchestrator/job.py`
- Modify: `tests/test_job.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_job.py`:

```python
def test_job_default_title_is_none():
    j = Job(prompt="do something")
    assert j.title is None

def test_job_title_roundtrips():
    j = Job(prompt="do something", title="My custom title")
    d = j.to_dict()
    assert d["title"] == "My custom title"
    j2 = Job.from_dict(d)
    assert j2.title == "My custom title"

def test_job_title_missing_from_dict_defaults_none():
    j = Job(prompt="p", id="x")
    d = j.to_dict()
    del d["title"]
    j2 = Job.from_dict(d)
    assert j2.title is None

def test_job_token_fields_default_zero():
    j = Job(prompt="p")
    assert j.input_tokens == 0
    assert j.output_tokens == 0

def test_job_token_fields_roundtrip():
    j = Job(prompt="p")
    j.input_tokens = 1234
    j.output_tokens = 567
    d = j.to_dict()
    j2 = Job.from_dict(d)
    assert j2.input_tokens == 1234
    assert j2.output_tokens == 567
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_job.py -v -k "title or token"
```

Expected: FAIL — `TypeError: Job.__init__() got unexpected keyword argument 'title'`

- [ ] **Step 3: Add fields to Job model**

In `orchestrator/job.py`, replace the `Job` dataclass fields block:

```python
@dataclass
class Job:
    prompt: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: Optional[str] = None                # user-editable display name
    state: JobState = JobState.QUEUED
    session_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    error: Optional[str] = None
    resume_count: int = 0
    working_dir: Optional[str] = None
    input_tokens: int = 0                      # cumulative across all runs
    output_tokens: int = 0
```

- [ ] **Step 4: Update `to_dict` to include new fields**

In `orchestrator/job.py`, replace the `to_dict` method:

```python
def to_dict(self) -> dict:
    return {
        "id": self.id,
        "prompt": self.prompt,
        "title": self.title,
        "state": self.state.value,
        "session_id": self.session_id,
        "created_at": self.created_at,
        "updated_at": self.updated_at,
        "error": self.error,
        "resume_count": self.resume_count,
        "working_dir": self.working_dir,
        "input_tokens": self.input_tokens,
        "output_tokens": self.output_tokens,
    }
```

- [ ] **Step 5: Update `from_dict` to load new fields**

In `orchestrator/job.py`, replace the `from_dict` classmethod:

```python
@classmethod
def from_dict(cls, d: dict) -> "Job":
    j = cls(prompt=d["prompt"], id=d["id"])
    j.title = d.get("title")
    j.state = JobState(d["state"])
    j.session_id = d.get("session_id")
    j.created_at = d["created_at"]
    j.updated_at = d["updated_at"]
    j.error = d.get("error")
    j.resume_count = d.get("resume_count", 0)
    j.working_dir = d.get("working_dir")
    j.input_tokens = d.get("input_tokens", 0)
    j.output_tokens = d.get("output_tokens", 0)
    return j
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_job.py -v
```

Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add orchestrator/job.py tests/test_job.py
git commit -m "feat: add title, input_tokens, output_tokens fields to Job model"
```

---

## Task 2: Token tracking — extract usage from result events in runner

**Files:**
- Modify: `orchestrator/runner.py`
- Create: `tests/test_stats.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_stats.py`:

```python
import io
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from orchestrator.job import Job
from orchestrator.runner import run_job
from orchestrator.store import Store


@pytest.fixture
def tmp_store(tmp_path):
    return Store(db_path=tmp_path / "test.db")


def _make_result_line(input_tokens=100, output_tokens=50):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


@pytest.mark.asyncio
async def test_run_job_accumulates_tokens(tmp_store):
    job = Job(prompt="hello")
    tmp_store.save_job(job)
    assert job.input_tokens == 0
    assert job.output_tokens == 0

    result_line = _make_result_line(input_tokens=200, output_tokens=80)
    lines = [
        b'{"type":"system","session_id":"sess1"}\n',
        result_line.encode() + b"\n",
    ]

    async def fake_readline():
        if lines:
            return lines.pop(0)
        return b""

    mock_proc = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = fake_readline
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock()

    broadcast = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await run_job(job, tmp_store, broadcast)

    assert job.input_tokens == 200
    assert job.output_tokens == 80


@pytest.mark.asyncio
async def test_run_job_accumulates_tokens_across_resumes(tmp_store):
    job = Job(prompt="hello")
    job.session_id = "sess1"
    job.input_tokens = 100
    job.output_tokens = 40
    tmp_store.save_job(job)

    result_line = _make_result_line(input_tokens=150, output_tokens=60)
    lines = [result_line.encode() + b"\n"]

    async def fake_readline():
        if lines:
            return lines.pop(0)
        return b""

    mock_proc = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = fake_readline
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock()

    broadcast = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await run_job(job, tmp_store, broadcast)

    assert job.input_tokens == 250   # 100 + 150
    assert job.output_tokens == 100  # 40 + 60
```

- [ ] **Step 2: Install pytest-asyncio if needed, run tests to verify they fail**

```bash
~/.local/bin/pip install pytest-asyncio
python3 -m pytest tests/test_stats.py -v
```

Expected: FAIL — tokens remain 0 after run

- [ ] **Step 3: Add token extraction to runner**

In `orchestrator/runner.py`, replace the result-event block inside `run_job`:

```python
        # Detect stop reason from result events; extract token usage
        if event_type == "result":
            stop_reason = classify_result_event(event)
            usage = event.get("usage") or {}
            job.input_tokens += int(usage.get("input_tokens", 0))
            job.output_tokens += int(usage.get("output_tokens", 0))
            store.save_job(job)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_stats.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/runner.py tests/test_stats.py
git commit -m "feat: accumulate input/output tokens from result events on Job"
```

---

## Task 3: Scheduler stats counter + GET /api/stats endpoint

**Files:**
- Modify: `orchestrator/scheduler.py`
- Modify: `web/server.py`

- [ ] **Step 1: Add `processes_spawned` counter to Scheduler**

In `orchestrator/scheduler.py`, in `__init__`, add after `self._pending_msgs`:

```python
        self.processes_spawned: int = 0  # total claude subprocesses ever launched
```

In `orchestrator/scheduler.py`, in `_lifecycle`, after `stop_reason = await run_job(...)`, increment by wrapping the call. Replace:

```python
                stop_reason = await run_job(job, self._store, self._broadcast)
                await self._handle_stop(job, stop_reason)
```

with:

```python
                self.processes_spawned += 1
                stop_reason = await run_job(job, self._store, self._broadcast)
                await self._handle_stop(job, stop_reason)
```

Also increment in `_wait_and_resume` — replace:

```python
        stop_reason = await run_job(job, self._store, self._broadcast)
```

with:

```python
        self.processes_spawned += 1
        stop_reason = await run_job(job, self._store, self._broadcast)
```

- [ ] **Step 2: Add `stats()` method to Scheduler**

Add this method to `Scheduler` in `orchestrator/scheduler.py`, after `queue_status`:

```python
    def stats(self) -> dict:
        """Return live server statistics."""
        all_jobs = self._store.list_jobs()
        running = sum(1 for j in all_jobs if j.state.value == "running")
        total_input = sum(j.input_tokens for j in all_jobs)
        total_output = sum(j.output_tokens for j in all_jobs)
        return {
            "processes_spawned": self.processes_spawned,
            "jobs_running": running,
            "total_jobs": len(all_jobs),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        }
```

- [ ] **Step 3: Add `GET /api/stats` endpoint to `web/server.py`**

Add after the `get_queue` endpoint:

```python
@app.get("/api/stats")
async def get_stats():
    return scheduler.stats()
```

- [ ] **Step 4: Add `PATCH /api/jobs/{id}` endpoint for title editing**

Add a Pydantic model and endpoint to `web/server.py` after the `get_job` endpoint:

```python
class PatchJobRequest(BaseModel):
    title: str | None = None


@app.patch("/api/jobs/{job_id}")
async def patch_job(job_id: str, req: PatchJobRequest):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if req.title is not None:
        job.title = req.title.strip() or None
        job.updated_at = __import__("datetime").datetime.utcnow().isoformat()
        store.save_job(job)
    return job.to_dict()
```

- [ ] **Step 5: Verify the server starts without errors**

```bash
python3 -m web.server &
sleep 1
curl -s http://localhost:8888/api/stats
kill %1
```

Expected output (values will vary):
```json
{"processes_spawned":0,"jobs_running":0,"total_jobs":0,"total_input_tokens":0,"total_output_tokens":0}
```

- [ ] **Step 6: Commit**

```bash
git add orchestrator/scheduler.py web/server.py
git commit -m "feat: add stats() to scheduler, GET /api/stats and PATCH /api/jobs/{id} endpoints"
```

---

## Task 4: HTML — title input in modal, stats bar in sidebar, collapse toggle

**Files:**
- Modify: `web/static/index.html`

- [ ] **Step 1: Add title input to New Job modal**

In `web/static/index.html`, replace the modal content:

```html
<div id="modal">
    <h2>New Job</h2>

    <label for="title-input">Title <span style="color:var(--text-muted)">(optional)</span></label>
    <input type="text" id="title-input" placeholder="Short name for this job…" />

    <label for="prompt-input">Task prompt <span style="color:var(--text-muted)">(Ctrl+Enter to submit)</span></label>
    <div id="modal-chips" class="attachment-chips"></div>
    <textarea id="prompt-input" placeholder="Describe the coding task for Claude Code…"></textarea>
    <input type="file" id="modal-file-input" multiple style="display:none">

    <label for="cwd-input">Working directory <span style="color:var(--text-muted)">(optional, defaults to home)</span></label>
    <input type="text" id="cwd-input" placeholder="/home/user/myproject" />

    <div class="modal-actions">
      <button class="btn btn-ghost" id="modal-cancel">Cancel</button>
      <button class="btn btn-ghost btn-attach" id="modal-attach" title="Attach files">📎</button>
      <button class="btn btn-primary" id="modal-submit">Start Job</button>
    </div>
  </div>
```

- [ ] **Step 2: Add sidebar collapse toggle button and stats bar**

In `web/static/index.html`, replace the entire `#sidebar` div:

```html
  <div id="sidebar">
    <div id="sidebar-header">
      <div class="logo">C</div>
      <div class="sidebar-header-text">
        <h1>Orchestrator</h1>
        <span>Claude Code Supervisor</span>
      </div>
      <button id="sidebar-toggle" title="Collapse sidebar">‹</button>
    </div>
    <button id="new-job-btn">+ New Job</button>
    <div id="job-list"></div>
    <div id="stats-bar">
      <div class="stat-item" title="Total claude subprocesses launched this session">
        <span class="stat-label">Spawned</span>
        <span class="stat-value" id="stat-spawned">0</span>
      </div>
      <div class="stat-item" title="Jobs currently running">
        <span class="stat-label">Running</span>
        <span class="stat-value" id="stat-running">0</span>
      </div>
      <div class="stat-item" title="Total input tokens consumed across all jobs">
        <span class="stat-label">In tok</span>
        <span class="stat-value" id="stat-in-tok">0</span>
      </div>
      <div class="stat-item" title="Total output tokens produced across all jobs">
        <span class="stat-label">Out tok</span>
        <span class="stat-value" id="stat-out-tok">0</span>
      </div>
    </div>
  </div>
```

- [ ] **Step 3: Add inline title edit area to detail panel**

In `web/static/index.html`, replace the `detail-row-prompt` div:

```html
        <div class="detail-row detail-row-prompt">
          <div class="detail-title-wrap">
            <div id="detail-title" class="detail-title"></div>
            <button id="edit-title-btn" class="btn-edit-title" title="Edit title">✎</button>
            <input id="edit-title-input" class="edit-title-input" type="text" style="display:none" />
          </div>
          <div id="detail-prompt"></div>
          <div class="detail-actions">
            <button id="retry-btn" class="btn-action btn-retry" title="Re-run this prompt as a new job">↺ Retry</button>
            <button id="cancel-btn" class="btn-action btn-cancel" title="Cancel running job">✕ Cancel</button>
          </div>
        </div>
```

- [ ] **Step 4: Add token meta chip to detail panel**

In `web/static/index.html`, add a chip after the `meta-resumes-wrap` chip inside `.detail-row-meta`:

```html
          <div class="meta-chip" id="meta-tokens-wrap">
            <span class="meta-label">Tokens</span>
            <span id="meta-tokens"></span>
          </div>
```

- [ ] **Step 5: Commit**

```bash
git add web/static/index.html
git commit -m "feat: add title input, stats bar, collapse toggle, token chip to HTML"
```

---

## Task 5: CSS — stats bar, collapsed sidebar, inline edit, title styles

**Files:**
- Modify: `web/static/style.css`

- [ ] **Step 1: Add all new styles to the end of `web/static/style.css`**

```css
/* ── Sidebar collapse ─────────────────────────────────────────────── */

#sidebar {
  transition: width 0.2s ease, min-width 0.2s ease;
}

#sidebar.collapsed {
  width: 48px;
  min-width: 48px;
}

#sidebar.collapsed .sidebar-header-text,
#sidebar.collapsed #new-job-btn,
#sidebar.collapsed #job-list,
#sidebar.collapsed #stats-bar {
  display: none;
}

#sidebar.collapsed .logo { margin: 0 auto; }
#sidebar.collapsed #sidebar-toggle { transform: rotate(180deg); }

#sidebar-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sidebar-header-text { flex: 1; }
.sidebar-header-text h1 { font-size: 14px; font-weight: 600; color: var(--text); letter-spacing: 0.01em; }
.sidebar-header-text span { font-size: 11px; color: var(--text-dim); }

#sidebar-toggle {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 18px;
  line-height: 1;
  padding: 2px 4px;
  flex-shrink: 0;
  transition: transform 0.2s ease, color 0.15s;
}
#sidebar-toggle:hover { color: var(--text); }

/* ── Stats bar ────────────────────────────────────────────────────── */

#stats-bar {
  border-top: 1px solid var(--border);
  padding: 8px 12px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 8px;
}

.stat-item {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.stat-label {
  font-size: 9px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
}

.stat-value {
  font-size: 13px;
  font-family: var(--font-mono);
  color: var(--accent);
  font-weight: 600;
}

/* ── Detail panel title + inline edit ────────────────────────────── */

.detail-title-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.detail-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.btn-edit-title {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
  padding: 1px 4px;
  border-radius: 3px;
  flex-shrink: 0;
  transition: color 0.15s, background 0.15s;
}
.btn-edit-title:hover { color: var(--accent); background: rgba(212,168,67,0.08); }

.edit-title-input {
  flex: 1;
  background: var(--bg-input);
  border: 1px solid var(--accent-dim);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
  padding: 3px 8px;
  outline: none;
}
```

- [ ] **Step 2: Remove the old sidebar-header rules that are now duplicated**

Find and remove these lines from the existing CSS (they were previously standalone; the new rules above replace them):

```css
#sidebar-header h1 { font-size: 14px; font-weight: 600; color: var(--text); letter-spacing: 0.01em; }
#sidebar-header span { font-size: 11px; color: var(--text-dim); }
```

- [ ] **Step 3: Commit**

```bash
git add web/static/style.css
git commit -m "feat: add stats bar, sidebar collapse, and inline title edit CSS"
```

---

## Task 6: JavaScript — title, stats, sidebar collapse, token chip

**Files:**
- Modify: `web/static/app.js`

- [ ] **Step 1: Add new DOM refs after the existing refs block**

After the line `const thinkingLabel = document.getElementById("thinking-label");`, add:

```js
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
```

- [ ] **Step 2: Add sidebar collapse logic**

After the DOM refs block, add:

```js
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
```

- [ ] **Step 3: Update `submitJob` to send title**

Replace the `submitJob` function:

```js
async function submitJob() {
  const rawPrompt = promptInput.value.trim();
  if (!rawPrompt) { promptInput.focus(); return; }
  const working_dir = cwdInput.value.trim() || null;
  const title = titleInput ? titleInput.value.trim() || null : null;
  const paths = modalAttachments.consumePaths();
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
```

- [ ] **Step 4: Update `StartJobRequest` on the server to accept title**

In `web/server.py`, replace:

```python
class StartJobRequest(BaseModel):
    prompt: str
    working_dir: str | None = None
```

with:

```python
class StartJobRequest(BaseModel):
    prompt: str
    working_dir: str | None = None
    title: str | None = None
```

And update the `start_job` endpoint:

```python
@app.post("/api/jobs")
async def start_job(req: StartJobRequest):
    job = Job(prompt=req.prompt, working_dir=req.working_dir, title=req.title or None)
    scheduler.start_job(job)
    return job.to_dict()
```

- [ ] **Step 5: Update `closeModal` to clear title input**

Replace `closeModal`:

```js
function closeModal() {
  modalOverlay.classList.remove("open");
  promptInput.value = "";
  cwdInput.value = "";
  if (titleInput) titleInput.value = "";
  modalAttachments.clear();
}
```

- [ ] **Step 6: Update `openModal` prefill to include title**

Replace `openModal`:

```js
function openModal(prefill) {
  modalOverlay.classList.add("open");
  if (prefill) {
    promptInput.value = prefill.prompt || "";
    cwdInput.value = prefill.working_dir || "";
    if (titleInput) titleInput.value = prefill.title || "";
  }
  promptInput.focus();
}
```

- [ ] **Step 7: Update `renderJobList` to show title instead of prompt snippet**

Replace the prompt line inside `renderJobList`:

```js
    const label = job.title || (job.prompt.length > 55 ? job.prompt.slice(0, 52) + "…" : job.prompt);
```

And replace the `el.innerHTML` prompt line:

```js
      <div class="job-item-prompt">${escHtml(label)}</div>
```

- [ ] **Step 8: Update `renderDetailPanel` to show title, token chip, and wire inline edit**

Replace `renderDetailPanel`:

```js
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
```

- [ ] **Step 9: Add `fmtNum` helper and inline title edit wiring**

Add after the `fmtTime` function:

```js
function fmtNum(n) {
  if (!n) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}
```

Add inline title edit logic after the `fmtNum` function:

```js
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
```

- [ ] **Step 10: Add stats polling**

Add after the inline title edit block:

```js
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
```

- [ ] **Step 11: Run all tests**

```bash
python3 -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 12: Commit**

```bash
git add web/static/app.js web/server.py
git commit -m "feat: job titles, stats panel, token chip, sidebar collapse — JS + server"
```

---

## Task 7: Manual smoke test

- [ ] **Step 1: Start the server and open the app**

```bash
python3 -m web.server
```

Open http://localhost:8888

- [ ] **Step 2: Test sidebar collapse**
1. Click the `‹` button on the sidebar header — sidebar should collapse to 48px with only the `C` logo visible
2. Refresh the page — sidebar should stay collapsed (localStorage persisted)
3. Click `‹` again — sidebar expands back

- [ ] **Step 3: Test job title in New Job modal**
1. Click "+ New Job"
2. Fill in Title: `"Test job"`, Prompt: `"echo hello"`
3. Submit — job appears in sidebar with title `"Test job"` instead of prompt snippet

- [ ] **Step 4: Test inline title editing**
1. Click the `✎` pencil button next to the title in the detail panel
2. Input appears — change to `"Renamed job"`, press Enter
3. Title updates in detail panel and sidebar immediately

- [ ] **Step 5: Test stats bar**
1. Verify the 4 stats (Spawned, Running, In tok, Out tok) are visible at the bottom of the sidebar
2. Start a job — "Running" increments to 1
3. After job completes, check that token counts are non-zero

- [ ] **Step 6: Final regression test**

```bash
python3 -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "feat: UX improvements — titles, stats, tokens, collapsible sidebar complete"
```

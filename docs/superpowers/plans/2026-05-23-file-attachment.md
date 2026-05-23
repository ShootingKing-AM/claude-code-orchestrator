# File/Image Attachment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add paste, drag-and-drop, and file-picker attachment support to both the New Job modal and the message bar, uploading files to the server immediately and injecting their paths into prompts on send.

**Architecture:** A new `POST /api/upload` FastAPI endpoint saves files to `~/.orch/uploads/<uuid>/<filename>` and returns their paths. The frontend manages an `attachments` array per input context, rendered as filename chips above each input. On send, file paths are appended to the prompt/message text.

**Tech Stack:** Python 3.10 / FastAPI (backend), vanilla JS (frontend), pytest (tests)

---

## File Map

| File | Action | What changes |
|---|---|---|
| `web/server.py` | Modify | Add `POST /api/upload` endpoint; add startup cleanup of old uploads |
| `web/static/index.html` | Modify | Add chip containers + paperclip buttons to modal and msg-bar |
| `web/static/style.css` | Modify | Styles for `.attachment-chips`, chip states, paperclip button, drag-over highlight |
| `web/static/app.js` | Modify | `Attachments` helper class; wire drag/paste/picker to both inputs; inject paths on send |
| `tests/test_upload.py` | Create | Tests for the upload endpoint |

---

## Task 1: Upload endpoint (backend)

**Files:**
- Modify: `web/server.py`
- Create: `tests/test_upload.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_upload.py`:

```python
import io
import os
import pytest
from fastapi.testclient import TestClient
from web.server import app

client = TestClient(app)


def test_upload_single_file():
    data = {"files": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")}
    res = client.post("/api/upload", files=data)
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["name"] == "hello.txt"
    assert body[0]["path"].endswith("hello.txt")
    assert os.path.exists(body[0]["path"])


def test_upload_multiple_files():
    files = [
        ("files", ("a.txt", io.BytesIO(b"aaa"), "text/plain")),
        ("files", ("b.txt", io.BytesIO(b"bbb"), "text/plain")),
    ]
    res = client.post("/api/upload", files=files)
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 2
    names = {r["name"] for r in body}
    assert names == {"a.txt", "b.txt"}


def test_upload_no_files_returns_empty():
    res = client.post("/api/upload", files=[])
    assert res.status_code == 200
    assert res.json() == []


def test_duplicate_filename_no_collision():
    data1 = {"files": ("dup.txt", io.BytesIO(b"v1"), "text/plain")}
    data2 = {"files": ("dup.txt", io.BytesIO(b"v2"), "text/plain")}
    r1 = client.post("/api/upload", files=data1).json()
    r2 = client.post("/api/upload", files=data2).json()
    assert r1[0]["path"] != r2[0]["path"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_upload.py -v
```

Expected: FAIL — `404 Not Found` on `/api/upload`

- [ ] **Step 3: Add the upload endpoint to `web/server.py`**

Add these imports at the top of `web/server.py` (after existing imports):

```python
import shutil
import uuid
from datetime import datetime, timedelta
from fastapi import File, UploadFile
from typing import List
```

Add the uploads directory constant and cleanup helper after `store = Store()`:

```python
_UPLOADS_DIR = Path.home() / ".orch" / "uploads"
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_old_uploads() -> None:
    cutoff = datetime.now() - timedelta(hours=24)
    for subdir in _UPLOADS_DIR.iterdir():
        if subdir.is_dir():
            mtime = datetime.fromtimestamp(subdir.stat().st_mtime)
            if mtime < cutoff:
                shutil.rmtree(subdir, ignore_errors=True)
```

In the `_startup` function, add the cleanup call:

```python
@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(level=logging.INFO)
    scheduler.resume_paused_jobs()
    _cleanup_old_uploads()
```

Add the upload endpoint after the existing REST endpoints (before the SSE section):

```python
@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(default=[])):
    results = []
    for file in files:
        subdir = _UPLOADS_DIR / str(uuid.uuid4())
        subdir.mkdir(parents=True)
        dest = subdir / (file.filename or "upload")
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        results.append({"name": file.filename or "upload", "path": str(dest)})
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_upload.py -v
```

Expected: all 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add web/server.py tests/test_upload.py
git commit -m "feat: add POST /api/upload endpoint with 24h cleanup"
```

---

## Task 2: HTML structure — chip containers and paperclip buttons

**Files:**
- Modify: `web/static/index.html`

- [ ] **Step 1: Add chip container and paperclip button to the New Job modal**

In `web/static/index.html`, replace the modal's textarea block:

```html
<label for="prompt-input">Task prompt <span style="color:var(--text-muted)">(Ctrl+Enter to submit)</span></label>
<textarea id="prompt-input" placeholder="Describe the coding task for Claude Code…"></textarea>
```

with:

```html
<label for="prompt-input">Task prompt <span style="color:var(--text-muted)">(Ctrl+Enter to submit)</span></label>
<div id="modal-chips" class="attachment-chips"></div>
<textarea id="prompt-input" placeholder="Describe the coding task for Claude Code…"></textarea>
<input type="file" id="modal-file-input" multiple style="display:none">
```

Also replace the `modal-actions` div:

```html
<div class="modal-actions">
  <button class="btn btn-ghost" id="modal-cancel">Cancel</button>
  <button class="btn btn-primary" id="modal-submit">Start Job</button>
</div>
```

with:

```html
<div class="modal-actions">
  <button class="btn btn-ghost" id="modal-cancel">Cancel</button>
  <button class="btn btn-ghost btn-attach" id="modal-attach" title="Attach files">📎</button>
  <button class="btn btn-primary" id="modal-submit">Start Job</button>
</div>
```

- [ ] **Step 2: Add chip container and paperclip button to the message bar**

In `web/static/index.html`, replace the `#msg-bar` div:

```html
<div id="msg-bar">
  <input id="msg-input" type="text" placeholder="Send a message to Claude… (Enter to send)" autocomplete="off" />
  <button id="msg-send">Send</button>
</div>
```

with:

```html
<div id="msg-bar">
  <div id="msg-bar-inner">
    <div id="msg-chips" class="attachment-chips"></div>
    <div id="msg-bar-row">
      <input id="msg-input" type="text" placeholder="Send a message to Claude… (Enter to send)" autocomplete="off" />
      <input type="file" id="msg-file-input" multiple style="display:none">
      <button class="btn-attach" id="msg-attach" title="Attach files">📎</button>
      <button id="msg-send">Send</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Verify HTML renders without errors**

```bash
python3 -m web.server &
sleep 1
curl -s http://localhost:8888/ | grep -c "attachment-chips"
kill %1
```

Expected: `2` (two chip containers found)

- [ ] **Step 4: Commit**

```bash
git add web/static/index.html
git commit -m "feat: add attachment chip containers and paperclip buttons to HTML"
```

---

## Task 3: CSS — chip styles and drag-over highlight

**Files:**
- Modify: `web/static/style.css`

- [ ] **Step 1: Add all attachment styles to the end of `web/static/style.css`**

```css
/* ── Attachment chips ─────────────────────────────────────────────── */

.attachment-chips {
  display: none;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 0 2px;
}
.attachment-chips.has-chips { display: flex; }

.attach-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: var(--bg-card);
  border: 1px solid var(--border-light);
  border-radius: 99px;
  padding: 3px 10px 3px 8px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-dim);
  max-width: 220px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.attach-chip.uploading {
  border-color: var(--border);
  color: var(--text-muted);
  font-style: italic;
}
.attach-chip.error {
  border-color: var(--red);
  color: var(--red);
  background: rgba(224, 90, 90, 0.08);
}
.attach-chip-icon { flex-shrink: 0; }
.attach-chip-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}
.attach-chip-remove {
  flex-shrink: 0;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 12px;
  padding: 0;
  line-height: 1;
  transition: color 0.15s;
}
.attach-chip-remove:hover { color: var(--text); }

/* Drag-over highlight */
.drag-over {
  border-color: var(--accent) !important;
  background: rgba(212, 168, 67, 0.05) !important;
}

/* Paperclip button */
.btn-attach {
  background: none;
  border: 1px solid var(--border-light);
  border-radius: var(--radius);
  color: var(--text-dim);
  cursor: pointer;
  font-size: 14px;
  padding: 5px 9px;
  flex-shrink: 0;
  transition: border-color 0.15s, color 0.15s;
}
.btn-attach:hover { border-color: var(--accent-dim); color: var(--accent); }

/* Msg bar inner layout to stack chips above the input row */
#msg-bar-inner { flex: 1; display: flex; flex-direction: column; min-width: 0; }
#msg-bar-row   { display: flex; align-items: center; gap: 8px; }
#msg-bar-row #msg-input { flex: 1; }
```

- [ ] **Step 2: Commit**

```bash
git add web/static/style.css
git commit -m "feat: add attachment chip and drag-over CSS styles"
```

---

## Task 4: JavaScript — Attachments helper class

**Files:**
- Modify: `web/static/app.js`

- [ ] **Step 1: Add the `Attachments` class to `app.js`**

Insert this block near the top of `web/static/app.js`, after the DOM refs section (after the line `const thinkingLabel = document.getElementById("thinking-label");`):

```js
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
    const item = { name, path: null, chipEl: chip };
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
```

- [ ] **Step 2: Instantiate Attachments for both inputs in the Init section**

At the very end of `app.js`, replace:

```js
// ── Init ──────────────────────────────────────────────────────────────────────
loadJobList();
```

with:

```js
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
```

- [ ] **Step 3: Commit**

```bash
git add web/static/app.js
git commit -m "feat: add Attachments helper class with drag/drop/paste/picker support"
```

---

## Task 5: Wire attachments into send — modal and message bar

**Files:**
- Modify: `web/static/app.js`

- [ ] **Step 1: Inject file paths in `submitJob`**

In `app.js`, replace the `submitJob` function:

```js
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
```

with:

```js
async function submitJob() {
  const rawPrompt = promptInput.value.trim();
  if (!rawPrompt) { promptInput.focus(); return; }
  const working_dir = cwdInput.value.trim() || null;
  const paths = modalAttachments.consumePaths();
  const prompt = paths.length
    ? rawPrompt + "\n\nAttached files:\n" + paths.map(p => "- " + p).join("\n")
    : rawPrompt;
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
```

- [ ] **Step 2: Clear modal attachments when modal closes**

Replace the `closeModal` function:

```js
function closeModal() {
  modalOverlay.classList.remove("open");
  promptInput.value = "";
  cwdInput.value = "";
}
```

with:

```js
function closeModal() {
  modalOverlay.classList.remove("open");
  promptInput.value = "";
  cwdInput.value = "";
  modalAttachments.clear();
}
```

- [ ] **Step 3: Inject file paths in `sendMessage`**

In `app.js`, replace the line inside `sendMessage` that reads the text:

```js
  const text = msgInput.value.trim();
  if (!text || !activeJobId) return;
```

with:

```js
  const rawText = msgInput.value.trim();
  const paths = msgAttachments.consumePaths();
  const text = paths.length
    ? rawText + "\n\nAttached files:\n" + paths.map(p => "- " + p).join("\n")
    : rawText;
  if (!text || !activeJobId) return;
```

- [ ] **Step 4: Commit**

```bash
git add web/static/app.js
git commit -m "feat: inject attachment paths into job prompt and message on send"
```

---

## Task 6: Manual smoke test

- [ ] **Step 1: Start the server**

```bash
python3 -m web.server
```

Open http://localhost:8888 in a browser.

- [ ] **Step 2: Test New Job modal — drag and drop**

1. Click "+ New Job"
2. Drag any file onto the prompt textarea
3. Verify a chip appears above the textarea: `📎 filename ✕`
4. Click ✕ — chip should disappear
5. Drag the file again, then click "Start Job" without typing a prompt — job should not submit (empty prompt guard)
6. Type a prompt, attach the file, click "Start Job"
7. In the job detail view, verify the prompt shown includes `Attached files:` with the correct path

- [ ] **Step 3: Test New Job modal — paste**

1. Copy an image to clipboard (e.g. screenshot with Print Screen)
2. Open New Job modal, paste into the textarea
3. Verify chip appears with the image filename

- [ ] **Step 4: Test New Job modal — file picker**

1. Open New Job modal, click the 📎 button
2. Select a file — chip should appear

- [ ] **Step 5: Test message bar**

1. Open any job that has a session_id (so msg-bar is visible)
2. Drag a file onto the message input
3. Verify chip appears above the input
4. Send the message — verify the message in the output stream includes `Attached files:`

- [ ] **Step 6: Run all tests to confirm nothing regressed**

```bash
python3 -m pytest tests/ -v
```

Expected: all tests pass

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "feat: file attachment — smoke tested and complete"
```

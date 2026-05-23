# File/Image Attachment — Design Spec

**Date:** 2026-05-23  
**Status:** Approved

## Summary

Add paste, drag-and-drop, and file-picker attachment support to both the New Job modal and the message bar. Files are uploaded to the server immediately on attach, saved to a temp directory, and their paths are injected into the prompt/message text on send. Any file type is supported.

---

## 1. Backend — Upload Endpoint

### New endpoint
`POST /api/upload` — accepts `multipart/form-data` with one or more files.

**Response:** array of uploaded file records (one entry per file in the request):
```json
[
  { "path": "/home/sk/.orch/uploads/abc123/screenshot.png", "name": "screenshot.png" }
]
```

### Storage
- Files saved to `~/.orch/uploads/<uuid4>/<original-filename>`
- UUID subdirectory per upload call prevents filename collisions
- No file type filtering — any file accepted
- No server-side size limit (browser memory is the practical cap)

### Cleanup
- On server startup, delete upload subdirectories older than 24 hours

---

## 2. Frontend — Shared Attachment State

Both the New Job modal (prompt textarea) and the message bar (msg-input) share the same attachment behavior via a reusable `Attachments` helper module in `app.js`.

**State per input context:**
```js
attachments = [{ name: string, path: string }]
```

**Three entry points to attach files:**
1. **Drag-and-drop** — drop files onto the textarea or msg-input
2. **Paste** — `Ctrl+V` / `Cmd+V` intercepts `clipboardData.files`
3. **File picker** — a 📎 paperclip button beside each input opens a native `<input type="file" multiple>`

On file selection, each file is immediately POSTed to `/api/upload`. A transient "uploading…" chip is shown during the request.

---

## 3. Frontend — Chip UI

A `<div class="attachment-chips">` sits above each input. It is hidden (`display:none`) when the attachments array is empty.

**Chip states:**
- **Uploading:** `📎 filename.png  uploading…` (spinner/muted style)
- **Ready:** `📎 filename.png  ✕` (clickable ✕ removes the chip and drops the entry)
- **Error:** `📎 filename.png  ✗` (red style, dismissible — path is not stored)

**Layout:** chips wrap horizontally, no scroll. Each chip is a small pill with the filename truncated at 30 chars if needed.

---

## 4. Send Integration

On submit (new job or send message), file paths from `attachments` are appended to the prompt/message text before the API call:

```
<user's original text>

Attached files:
- /home/sk/.orch/uploads/abc123/screenshot.png
- /home/sk/.orch/uploads/abc123/notes.txt
```

After a successful send:
- `attachments` array is cleared
- Chip area is emptied and hidden

No changes to existing API contracts (`POST /api/jobs`, `POST /api/jobs/{id}/message`) — paths are plain text in the prompt/message body.

---

## 5. Error Handling & Edge Cases

| Scenario | Behaviour |
|---|---|
| Upload fails | Red error chip shown; no path stored; user can dismiss and retry |
| Duplicate filename | UUID subdirectory ensures no collision |
| Send with no attachments | Behaves exactly as today — no change |
| Server restart | `~/.orch/uploads/` persists; paths remain valid for in-flight jobs |
| Very large file | Browser will be slow to encode/send; no explicit cap, user is responsible |

---

## 6. Files Changed

| File | Change |
|---|---|
| `web/server.py` | Add `POST /api/upload` endpoint; add startup cleanup of old uploads |
| `web/static/app.js` | Add `Attachments` helper; wire drag/paste/picker to both inputs; chip UI; inject paths on send |
| `web/static/index.html` | Add chip containers and paperclip buttons to modal and msg-bar |
| `web/static/style.css` | Styles for `.attachment-chips`, chip states (uploading/ready/error), paperclip button |

"""FastAPI server: REST API + SSE streaming for the orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, List

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from orchestrator.job import Job, JobState
from orchestrator.scheduler import Scheduler
from orchestrator.store import Store

log = logging.getLogger(__name__)

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Claude Code Orchestrator")
store = Store()

# Per-job subscriber queues: job_id → list[asyncio.Queue]
_subscribers: dict[str, list[asyncio.Queue]] = {}


async def broadcast(job_id: str, event: dict) -> None:
    """Push an event to all SSE subscribers watching this job."""
    queues = _subscribers.get(job_id, [])
    for q in queues:
        await q.put(event)


scheduler = Scheduler(store=store, broadcast=broadcast)

_UPLOADS_DIR = Path.home() / ".orch" / "uploads"
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_old_uploads() -> None:
    cutoff = datetime.now() - timedelta(hours=24)
    for subdir in _UPLOADS_DIR.iterdir():
        if subdir.is_dir():
            mtime = datetime.fromtimestamp(subdir.stat().st_mtime)
            if mtime < cutoff:
                shutil.rmtree(subdir, ignore_errors=True)


@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(level=logging.INFO)
    scheduler.resume_paused_jobs()
    _cleanup_old_uploads()


# ── Static files ──────────────────────────────────────────────────────────────

_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    return (_static_dir / "index.html").read_text()


# ── REST API ──────────────────────────────────────────────────────────────────

class StartJobRequest(BaseModel):
    prompt: str
    working_dir: str | None = None
    title: str | None = None


@app.post("/api/jobs")
async def start_job(req: StartJobRequest):
    job = Job(prompt=req.prompt, working_dir=req.working_dir, title=req.title or None)
    scheduler.start_job(job)
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs():
    jobs = store.list_jobs()
    last_seqs = store.last_seq_by_job()
    msg_counts = store.user_msg_count_by_job()
    result = []
    for j in jobs:
        d = j.to_dict()
        d["last_seq"] = last_seqs.get(j.id, 0)
        d["user_msg_count"] = msg_counts.get(j.id, 0)
        result.append(d)
    return result


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


class PatchJobRequest(BaseModel):
    title: str | None = None


@app.patch("/api/jobs/{job_id}")
async def patch_job(job_id: str, req: PatchJobRequest):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if req.title is not None:
        job.title = req.title.strip() or None
        job.updated_at = datetime.utcnow().isoformat()
        store.save_job(job)
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    cancelled = scheduler.cancel_job(job_id)
    return {"cancelled": cancelled}


@app.get("/api/queue")
async def get_queue():
    return scheduler.queue_status()


@app.get("/api/stats")
async def get_stats():
    return scheduler.stats()


class SendMessageRequest(BaseModel):
    message: str


@app.post("/api/jobs/{job_id}/message")
async def send_message(job_id: str, req: SendMessageRequest):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    sent = await scheduler.send_message(job_id, req.message)
    if not sent:
        raise HTTPException(status_code=400, detail="Job has no session yet — cannot send a message before Claude starts")
    return {"sent": True}


@app.get("/api/jobs/{job_id}/logs")
async def get_logs(job_id: str, after: int = -1):
    return store.get_logs(job_id, after_seq=after)


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(default=[])):
    results = []
    for file in files:
        subdir = _UPLOADS_DIR / str(uuid.uuid4())
        subdir.mkdir(parents=True)
        safe_name = Path(file.filename or "upload").name
        dest = subdir / safe_name
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        results.append({"name": file.filename or "upload", "path": str(dest)})
    return results


@app.get("/api/uploads/file")
async def serve_upload(path: str):
    """Serve a previously uploaded file by absolute path (must be under _UPLOADS_DIR)."""
    from fastapi.responses import FileResponse
    target = Path(path).resolve()
    if not str(target).startswith(str(_UPLOADS_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


# ── SSE streaming ─────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str, after: int = -1):
    """
    Server-Sent Events endpoint.
    Replays stored log lines (after `after` seq), then streams live events.
    """
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(job_id, []).append(queue)

    async def generator() -> AsyncIterator[dict]:
        try:
            # 1. Replay historical logs
            for entry in store.get_logs(job_id, after_seq=after):
                yield {"data": json.dumps(entry)}

            # If already terminal after replay, close immediately
            job_now = store.load_job(job_id)
            if job_now and job_now.is_terminal():
                yield {"data": json.dumps({"type": "stream_end", "state": job_now.state.value})}
                return

            # 2. Stream live events; close when orch_state terminal arrives
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield {"data": json.dumps(event)}

                    # After delivering an orch_state terminal event, drain then close
                    if event.get("type") == "orch_state" and event.get("state") in ("completed", "failed"):
                        while not queue.empty():
                            yield {"data": json.dumps(queue.get_nowait())}
                        yield {"data": json.dumps({"type": "stream_end", "state": event["state"]})}
                        break

                except asyncio.TimeoutError:
                    # Keep-alive ping + check terminal in case we missed the event
                    job_now = store.load_job(job_id)
                    if job_now and job_now.is_terminal():
                        while not queue.empty():
                            yield {"data": json.dumps(queue.get_nowait())}
                        yield {"data": json.dumps({"type": "stream_end", "state": job_now.state.value})}
                        break
                    yield {"data": json.dumps({"type": "ping"})}

        finally:
            subs = _subscribers.get(job_id, [])
            if queue in subs:
                subs.remove(queue)

    return EventSourceResponse(generator(), sep="\n")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run("web.server:app", host="0.0.0.0", port=8888, reload=False)


if __name__ == "__main__":
    main()

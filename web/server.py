"""FastAPI server: REST API + SSE streaming for the orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
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


@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(level=logging.INFO)
    scheduler.resume_paused_jobs()


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


@app.post("/api/jobs")
async def start_job(req: StartJobRequest):
    job = Job(prompt=req.prompt, working_dir=req.working_dir)
    scheduler.start_job(job)
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs():
    return [j.to_dict() for j in store.list_jobs()]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
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


class SendMessageRequest(BaseModel):
    message: str


@app.post("/api/jobs/{job_id}/message")
async def send_message(job_id: str, req: SendMessageRequest):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    sent = await scheduler.send_message(job_id, req.message)
    if not sent:
        raise HTTPException(status_code=400, detail="Job has no active session or is not in a messageable state")
    return {"sent": True}


@app.get("/api/jobs/{job_id}/logs")
async def get_logs(job_id: str, after: int = -1):
    return store.get_logs(job_id, after_seq=after)


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

            # 2. Stream live events until job is terminal
            while True:
                job_now = store.load_job(job_id)
                if job_now and job_now.is_terminal():
                    # Drain any remaining queued events
                    while not queue.empty():
                        event = queue.get_nowait()
                        yield {"data": json.dumps(event)}
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield {"data": json.dumps(event)}
                except asyncio.TimeoutError:
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

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
    # Explicitly drain now that we're in the running event loop.
    # resume_paused_jobs() only populates _queue; we must kick _drain from
    # an async context so _lifecycle tasks are properly scheduled.
    await scheduler._drain()


# ── Static files ──────────────────────────────────────────────────────────────

_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    return (_static_dir / "index.html").read_text()


# ── REST API ──────────────────────────────────────────────────────────────────

_VALID_EFFORT = {"low", "medium", "high", "xhigh", "max"}

class StartJobRequest(BaseModel):
    prompt: str
    working_dir: str | None = None
    title: str | None = None
    model: str | None = None
    max_turns: int | None = None
    effort: str | None = None


@app.post("/api/jobs")
async def start_job(req: StartJobRequest):
    effort = req.effort if req.effort in _VALID_EFFORT else None
    job = Job(
        prompt=req.prompt,
        working_dir=req.working_dir,
        title=req.title or None,
        model=req.model or None,
        max_turns=req.max_turns or None,
        effort=effort,
    )
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
    model: str | None = None
    max_turns: int | None = None
    effort: str | None = None


@app.patch("/api/jobs/{job_id}")
async def patch_job(job_id: str, req: PatchJobRequest):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    changed = False
    if req.title is not None:
        job.title = req.title.strip() or None
        changed = True
    if req.model is not None:
        job.model = req.model or None
        changed = True
    if req.max_turns is not None:
        job.max_turns = req.max_turns or None
        changed = True
    if req.effort is not None:
        job.effort = req.effort if req.effort in _VALID_EFFORT else None
        changed = True
    if changed:
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


@app.post("/api/jobs/{job_id}/force-resume")
async def force_resume_job(job_id: str):
    job = store.load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ok = scheduler.force_resume(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job is not waiting on a limit — nothing to resume")
    return {"force_resumed": True}


@app.get("/api/queue")
async def get_queue():
    return scheduler.queue_status()


@app.get("/api/stats")
async def get_stats():
    return scheduler.stats()


@app.get("/api/usage-window")
async def get_usage_window():
    """
    Rolling-window token consumption.
    Returns per-hour buckets for the last 24 hours, plus totals for 1h, 5h, 24h.
    Also returns last limit-hit events with reset times from error result events.
    """
    from datetime import datetime as _dt
    import sqlite3 as _sql

    now = _dt.utcnow()

    with store._connect() as conn:
        # All result events in the last 24 hours
        cutoff24 = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT ts, raw FROM logs WHERE event_type='result' AND ts >= ? ORDER BY ts ASC",
            (cutoff24,),
        ).fetchall()

    buckets: dict[str, dict] = {}  # hour_str -> {ctx, output, cost, calls}
    limit_hits = []
    window_totals = {1: {"ctx": 0, "output": 0, "cost": 0, "calls": 0},
                     5: {"ctx": 0, "output": 0, "cost": 0, "calls": 0},
                     24:{"ctx": 0, "output": 0, "cost": 0, "calls": 0}}

    for ts_str, raw in rows:
        try:
            ts = _dt.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        try:
            event = json.loads(raw)
        except Exception:
            continue

        usage = event.get("usage") or {}
        ctx = (int(usage.get("input_tokens", 0))
               + int(usage.get("cache_read_input_tokens", 0))
               + int(usage.get("cache_creation_input_tokens", 0)))
        out  = int(usage.get("output_tokens", 0))
        cost = float(event.get("total_cost_usd", 0))

        # Hour bucket (UTC)
        hour_key = ts.strftime("%Y-%m-%dT%H:00")
        b = buckets.setdefault(hour_key, {"ctx": 0, "output": 0, "cost": 0.0, "calls": 0})
        b["ctx"]    += ctx
        b["output"] += out
        b["cost"]   += cost
        b["calls"]  += 1

        # Rolling window totals
        age_hours = (now - ts).total_seconds() / 3600
        for window in (1, 5, 24):
            if age_hours <= window:
                window_totals[window]["ctx"]    += ctx
                window_totals[window]["output"] += out
                window_totals[window]["cost"]   += cost
                window_totals[window]["calls"]  += 1

        # Detect limit-hit events
        if event.get("api_error_status") == 429 or "session limit" in str(event.get("result", "")).lower():
            limit_hits.append({
                "ts": ts_str,
                "result_text": event.get("result", ""),
                "ctx": ctx,
            })

    # Build sorted list of hourly buckets for the last 24h (fill missing hours with zeros)
    hours_list = []
    for h in range(23, -1, -1):
        hour_dt = now - timedelta(hours=h)
        key = hour_dt.strftime("%Y-%m-%dT%H:00")
        b = buckets.get(key, {"ctx": 0, "output": 0, "cost": 0.0, "calls": 0})
        hours_list.append({
            "hour": key,
            "label": hour_dt.strftime("%H:00"),
            "ctx": b["ctx"],
            "output": b["output"],
            "cost": round(b["cost"], 4),
            "calls": b["calls"],
        })

    for window in window_totals:
        window_totals[window]["cost"] = round(window_totals[window]["cost"], 4)

    return {
        "now_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours": hours_list,
        "windows": {
            "1h":  window_totals[1],
            "5h":  window_totals[5],
            "24h": window_totals[24],
        },
        "limit_hits": limit_hits[-5:],  # last 5 limit events
    }


@app.get("/api/token-efficiency")
async def get_token_efficiency():
    """Per-job token breakdown with cache efficiency and burn rate.

    NOTE: In Claude's API, `input_tokens` = only uncached tokens (usually very small).
    Total context = input_tokens + cache_creation_input_tokens + cache_read_input_tokens.
    Rate limits are based on total context, not just input_tokens.
    Cache hit rate = cache_read / total_context.
    """
    jobs = store.list_jobs()
    rows = []
    for j in jobs:
        uncached   = j.input_tokens
        cache_read = j.cache_read_tokens
        cache_write= j.cache_creation_tokens
        # Total context processed (what actually counts toward rate limits)
        total_ctx  = uncached + cache_read + cache_write
        cache_hit_rate = round(cache_read / total_ctx, 4) if total_ctx > 0 else 0.0

        duration_mins = None
        burn_rate = None
        try:
            from datetime import datetime as _dt
            created = _dt.fromisoformat(j.created_at)
            updated = _dt.fromisoformat(j.updated_at)
            duration_mins = round((updated - created).total_seconds() / 60, 1)
            total_tokens = total_ctx + j.output_tokens
            if duration_mins > 0 and total_tokens > 0:
                burn_rate = round(total_tokens / duration_mins)
        except Exception:
            pass

        rows.append({
            "id": j.id,
            "title": j.title or j.prompt[:50],
            "state": j.state.value,
            "uncached_input_tokens": uncached,
            "output_tokens": j.output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_write,
            "total_context_tokens": total_ctx,
            "cache_hit_rate": cache_hit_rate,
            "total_cost_usd": round(j.total_cost_usd, 4),
            "resume_count": j.resume_count,
            "duration_mins": duration_mins,
            "burn_rate_per_min": burn_rate,
        })

    total_uncached     = sum(j.input_tokens for j in jobs)
    total_out          = sum(j.output_tokens for j in jobs)
    total_cache_read   = sum(j.cache_read_tokens for j in jobs)
    total_cache_write  = sum(j.cache_creation_tokens for j in jobs)
    total_ctx_all      = total_uncached + total_cache_read + total_cache_write
    overall_cache_hit  = round(total_cache_read / total_ctx_all, 4) if total_ctx_all > 0 else 0.0
    total_cost         = round(sum(j.total_cost_usd for j in jobs), 4)

    return {
        "jobs": rows,
        "totals": {
            "uncached_input_tokens": total_uncached,
            "output_tokens": total_out,
            "cache_read_tokens": total_cache_read,
            "cache_creation_tokens": total_cache_write,
            "total_context_tokens": total_ctx_all,
            "overall_cache_hit_rate": overall_cache_hit,
            "total_cost_usd": total_cost,
        },
    }


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

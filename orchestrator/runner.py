"""Spawn and stream a Claude Code process, yielding parsed events."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

from .detector import StopReason, classify_result_event, classify_line, extract_session_id
from .job import Job, JobState
from .store import Store

log = logging.getLogger(__name__)

# How long to wait between polling lines (seconds)
_READ_TIMEOUT = 0.1


async def run_job(job: Job, store: Store, broadcast, message: str | None = None) -> StopReason:
    """
    Spawn `claude` for the given job, stream events to the store and broadcast
    callback, and return the final StopReason.

    `broadcast` is an async callable(job_id, event_dict) used to push events
    to SSE subscribers.
    """
    cmd = _build_command(job, message=message)
    cwd = job.working_dir or str(Path.home())

    log.info("Starting claude: %s (cwd=%s)", " ".join(cmd), cwd)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )

    seq = 0
    stop_reason = StopReason.UNKNOWN

    async for raw_line in _read_lines(proc):
        seq += 1
        raw_line = raw_line.rstrip("\n")

        event = _parse_line(raw_line)
        event_type = event.get("type", "raw")

        # Capture session_id from system/init events
        if event_type in ("system", "init") and not job.session_id:
            sid = extract_session_id(event)
            if sid:
                job.session_id = sid
                store.save_job(job)

        # Detect stop reason from result events
        if event_type == "result":
            stop_reason = classify_result_event(event)

        # Fallback: scan raw line for limit patterns
        if stop_reason == StopReason.UNKNOWN:
            fallback = classify_line(raw_line)
            if fallback == StopReason.LIMIT_HIT:
                stop_reason = StopReason.LIMIT_HIT

        # Persist log line (full raw, including any binary data)
        store.append_log(job.id, seq, event_type, raw_line)

        # Broadcast a sanitised copy — strip large binary fields so SSE
        # chunks stay within reasonable line limits (base64 images can be MBs)
        broadcast_event = _sanitise_for_broadcast(event, seq, raw_line)
        await broadcast(job.id, broadcast_event)

    await proc.wait()

    # If process exited non-zero and we still don't know, mark as failed
    if stop_reason == StopReason.UNKNOWN:
        if proc.returncode != 0:
            stop_reason = StopReason.FAILED
        else:
            stop_reason = StopReason.COMPLETED

    log.info("Job %s finished with stop_reason=%s rc=%s", job.id, stop_reason, proc.returncode)
    return stop_reason


def _build_command(job: Job, message: str | None = None) -> list[str]:
    cmd = [
        "claude",
        "--output-format", "stream-json",
        "--verbose",
        "--print",
        "--dangerously-skip-permissions",
    ]
    if job.session_id:
        cmd += ["--resume", job.session_id, message or "continue"]
    else:
        cmd += [message or job.prompt]
    return cmd


async def send_message(job: Job, message: str, store: Store, broadcast) -> StopReason:
    """Send an additional message into an existing Claude session."""
    if not job.session_id:
        raise ValueError("Job has no session_id — cannot send message before session starts")
    return await run_job(job, store, broadcast, message=message)


# Fields that may contain large base64 blobs — strip before broadcasting
_BINARY_FIELDS = {"data", "image_data", "source", "content"}
_MAX_FIELD_BYTES = 2000  # truncate any string field over this in broadcast


def _sanitise_for_broadcast(event: dict, seq: int, raw_line: str) -> dict:
    """Return a copy of event safe to send over SSE (no large binary blobs)."""
    out: dict = {"seq": seq, "type": event.get("type", "raw")}
    for k, v in event.items():
        if k in _BINARY_FIELDS and isinstance(v, str) and len(v) > _MAX_FIELD_BYTES:
            out[k] = v[:_MAX_FIELD_BYTES] + "…[truncated]"
        elif isinstance(v, str) and len(v) > 50_000:
            # Any unexpectedly large string field
            out[k] = v[:_MAX_FIELD_BYTES] + "…[truncated]"
        else:
            out[k] = v
    # Always include the raw line (already length-limited by the field rules above
    # but we keep it so the frontend can re-parse the full type structure)
    out["raw"] = raw_line if len(raw_line) <= 50_000 else raw_line[:_MAX_FIELD_BYTES] + "…[truncated]"
    return out


def _parse_line(line: str) -> dict:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "raw", "raw": line}


async def _read_lines(proc: asyncio.subprocess.Process) -> AsyncIterator[str]:
    assert proc.stdout is not None
    while True:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            # Check if process is still alive
            if proc.returncode is not None:
                break
            continue
        if not line:
            break
        yield line.decode("utf-8", errors="replace")

"""
Scheduler: owns the job lifecycle loop with a serial queue.

Jobs are queued and run one at a time. While a job is running, new jobs
sit in QUEUED state. When the running job finishes (any terminal state),
the next queued job starts automatically.

Rate-limit pauses count as "still active" — the queue does not advance
while a paused job is waiting to resume.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from .detector import StopReason, parse_reset_time
from .job import Job, JobState
from .runner import run_job, send_message
from .store import Store

log = logging.getLogger(__name__)

LIMIT_RETRY_SECONDS   = int(60 * 60)
POLL_INTERVAL_SECONDS = 60   # update countdown every minute
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _to_ist(dt: datetime) -> datetime:
    """Convert a UTC datetime to IST (UTC+5:30)."""
    return dt + _IST_OFFSET


class Scheduler:
    def __init__(self, store: Store, broadcast) -> None:
        self._store     = store
        self._broadcast = broadcast
        self._queue: list[str] = []
        self._running: dict[str, asyncio.Task] = {}
        self._run_lock = asyncio.Lock()
        # Per-job lock prevents concurrent send_message calls on the same job
        self._msg_locks: dict[str, asyncio.Lock] = {}
        # Messages queued while a job is paused — drained and delivered on resume
        self._pending_msgs: dict[str, list[str]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def enqueue_job(self, job: Job) -> None:
        """Add a new job to the back of the queue."""
        self._store.save_job(job)
        self._queue.append(job.id)
        log.info("Job %s queued (position %d)", job.id, len(self._queue))
        asyncio.ensure_future(self._drain())

    # Keep old name as alias so server.py doesn't break
    def start_job(self, job: Job) -> None:
        self.enqueue_job(job)

    def cancel_job(self, job_id: str) -> bool:
        # Remove from queue if not yet started
        if job_id in self._queue:
            self._queue.remove(job_id)
            job = self._store.load_job(job_id)
            if job:
                job.error = "Cancelled while queued"
                job.transition(JobState.FAILED)
                self._store.save_job(job)
                asyncio.ensure_future(self._broadcast_state(job))
            return True
        # Cancel a running task
        task = self._running.get(job_id)
        if task:
            task.cancel()
            return True
        return False

    def queue_status(self) -> list[dict]:
        """Return ordered list of {job_id, position} for queued jobs."""
        return [{"job_id": jid, "position": i + 1} for i, jid in enumerate(self._queue)]

    async def send_message(self, job_id: str, message: str) -> bool:
        """
        Send a follow-up message into an existing Claude session.
        - If job is PAUSED_DUE_TO_LIMIT: queues the message; it will be sent
          automatically when the limit resets and the job resumes.
        - If job is running: queues behind the current message lock.
        - If job is terminal (completed/failed): reopens the session.
        """
        job = self._store.load_job(job_id)
        if not job or not job.session_id:
            return False

        # If job is paused waiting for limit reset, queue the message for later
        if job.state == JobState.PAUSED_DUE_TO_LIMIT:
            self._pending_msgs.setdefault(job_id, []).append(message)
            log.info("Job %s paused — queued message for when limit resets", job_id)
            # Persist as user_msg so it shows in history immediately
            from .runner import send_message as _runner_send_msg
            from datetime import datetime as _dt
            import json as _json
            user_raw = _json.dumps({"type": "user_msg", "text": message, "ts": _dt.utcnow().isoformat(), "queued": True})
            with self._store._connect() as conn:
                last = conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM logs WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO logs(job_id, seq, event_type, raw) VALUES(?,?,?,?)",
                    (job_id, last + 1, "user_msg", user_raw),
                )
            await self._broadcast(job_id, {"type": "user_msg", "text": message, "seq": last + 1, "queued": True})
            await self._broadcast(job_id, {"type": "orch_status", "message": "Message queued — will send when limit resets"})
            return True

        # Ensure a per-job lock exists and acquire it to prevent concurrent messages
        if job_id not in self._msg_locks:
            self._msg_locks[job_id] = asyncio.Lock()
        lock = self._msg_locks[job_id]
        if lock.locked():
            log.warning("Job %s already processing a message, dropping duplicate", job_id)
            return False

        async def _do_send():
            async with lock:
                # Re-read job fresh inside the lock
                j = self._store.load_job(job_id)
                if not j or not j.session_id:
                    return

                was_terminal = j.is_terminal()
                if was_terminal:
                    # Bypass the state machine to re-open a finished job
                    j.state = JobState.RUNNING
                    j.updated_at = datetime.utcnow().isoformat()
                    self._store.save_job(j)
                    await self._broadcast_state(j)

                stop = await send_message(j, message, self._store, self._broadcast)

                if was_terminal:
                    # Handle stop result and re-close the job
                    await self._handle_stop(j, stop)

        asyncio.ensure_future(_do_send())
        return True

    def resume_paused_jobs(self) -> None:
        """On startup, re-add paused jobs to the front of the queue."""
        paused = self._store.list_jobs_by_state(JobState.PAUSED_DUE_TO_LIMIT)
        for job in reversed(paused):  # preserve order
            log.info("Re-queuing paused job %s on startup", job.id)
            # Reset to queued so _lifecycle handles the wait-and-resume path
            self._queue.insert(0, job.id)
        if paused:
            asyncio.ensure_future(self._drain())

    # ── Queue drain ───────────────────────────────────────────────────────────

    async def _drain(self) -> None:
        """Pop the next queued job and run it if nothing is running."""
        if self._run_lock.locked():
            return  # another job is already running
        if not self._queue:
            return

        job_id = self._queue.pop(0)
        job    = self._store.load_job(job_id)
        if not job:
            await self._drain()  # skip ghost
            return

        task = asyncio.ensure_future(self._lifecycle(job))
        self._running[job_id] = task

        def _on_done(t: asyncio.Task) -> None:
            self._running.pop(job_id, None)
            asyncio.ensure_future(self._drain())   # start next job

        task.add_done_callback(_on_done)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _lifecycle(self, job: Job) -> None:
        async with self._run_lock:
            try:
                # If job was paused (resumed from startup), skip to wait-and-resume
                if job.state == JobState.PAUSED_DUE_TO_LIMIT:
                    await self._wait_and_resume(job)
                    return

                job.transition(JobState.RUNNING)
                self._store.save_job(job)
                await self._broadcast_state(job)
                await self._broadcast_queue_update()

                stop_reason = await run_job(job, self._store, self._broadcast)
                await self._handle_stop(job, stop_reason)

            except asyncio.CancelledError:
                log.info("Job %s was cancelled", job.id)
                job.error = "Cancelled by user"
                try:
                    job.transition(JobState.FAILED)
                except ValueError:
                    pass
                self._store.save_job(job)
                await self._broadcast_state(job)
                raise

            except Exception as exc:
                log.exception("Unexpected error in job %s", job.id)
                job.error = str(exc)
                try:
                    job.transition(JobState.FAILED)
                except ValueError:
                    pass
                self._store.save_job(job)
                await self._broadcast_state(job)

    async def _handle_stop(self, job: Job, stop_reason: StopReason, last_result_text: str = "") -> None:
        def _try_transition(new_state: JobState):
            try:
                job.transition(new_state)
            except ValueError:
                job.state = new_state
                job.updated_at = datetime.utcnow().isoformat()

        if stop_reason == StopReason.COMPLETED:
            _try_transition(JobState.COMPLETED)
            self._store.save_job(job)
            await self._broadcast_state(job)

        elif stop_reason == StopReason.LIMIT_HIT:
            _try_transition(JobState.PAUSED_DUE_TO_LIMIT)
            self._store.save_job(job)
            await self._broadcast_state(job)
            await self._wait_and_resume(job, last_result_text)

        else:
            _try_transition(JobState.FAILED)
            self._store.save_job(job)
            await self._broadcast_state(job)

    async def _wait_and_resume(self, job: Job, last_result_text: str = "") -> None:
        # Try to parse exact reset time from the limit message
        parsed_reset = parse_reset_time(last_result_text) if last_result_text else None
        if parsed_reset and parsed_reset <= datetime.utcnow():
            retry_at = datetime.utcnow()
        elif parsed_reset:
            retry_at = parsed_reset
        else:
            retry_at = datetime.utcnow() + timedelta(seconds=LIMIT_RETRY_SECONDS)

        retry_ist = _to_ist(retry_at)
        log.info("Job %s paused. Retrying at %s IST", job.id, retry_ist.strftime("%H:%M:%S"))

        remaining_secs = int((retry_at - datetime.utcnow()).total_seconds())
        if remaining_secs > 0:
            await self._broadcast(job.id, {
                "type": "orch_status",
                "message": f"Rate limit hit. Retrying at {retry_ist.strftime('%I:%M %p IST')} ({remaining_secs // 60}m)",
            })

        while datetime.utcnow() < retry_at:
            remaining = int((retry_at - datetime.utcnow()).total_seconds())
            await self._broadcast(job.id, {
                "type": "orch_status",
                "message": f"Waiting for limit reset… {remaining // 60}m {remaining % 60:02d}s remaining",
            })
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, max(remaining, 1)))

        job.resume_count += 1
        job.transition(JobState.RUNNING)
        self._store.save_job(job)
        await self._broadcast_state(job)

        # Drain any messages the user sent while we were waiting
        pending = self._pending_msgs.pop(job.id, [])

        stop_reason = await run_job(job, self._store, self._broadcast)

        # Send each queued message in sequence after the resume run_job finishes
        for queued_msg in pending:
            if stop_reason == StopReason.LIMIT_HIT:
                # Hit limit again — re-queue remaining messages and wait
                remaining_msgs = pending[pending.index(queued_msg):]
                self._pending_msgs.setdefault(job.id, []).extend(remaining_msgs)
                break
            log.info("Job %s delivering queued message after resume", job.id)
            stop_reason = await send_message(job, queued_msg, self._store, self._broadcast)

        await self._handle_stop(job, stop_reason)

    async def _broadcast_state(self, job: Job) -> None:
        await self._broadcast(job.id, {"type": "orch_state", "state": job.state.value})

    async def _broadcast_queue_update(self) -> None:
        """Broadcast current queue order to all jobs' subscribers."""
        status = self.queue_status()
        # Broadcast to each queued job so their UI can update position
        for item in status:
            await self._broadcast(item["job_id"], {
                "type": "orch_queue",
                "position": item["position"],
                "queue": status,
            })

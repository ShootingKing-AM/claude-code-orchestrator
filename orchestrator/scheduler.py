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

from .detector import StopReason
from .job import Job, JobState
from .runner import run_job, send_message
from .store import Store

log = logging.getLogger(__name__)

LIMIT_RETRY_SECONDS  = int(60 * 60)
POLL_INTERVAL_SECONDS = int(5 * 60)


class Scheduler:
    def __init__(self, store: Store, broadcast) -> None:
        self._store     = store
        self._broadcast = broadcast
        # Ordered queue of job IDs waiting to run
        self._queue: list[str] = []
        # Tasks for jobs that are actively running (including paused-waiting)
        self._running: dict[str, asyncio.Task] = {}
        # Lock: only one job may call run_job() at a time
        self._run_lock = asyncio.Lock()

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
        Send a follow-up message into a running Claude session.
        Returns False if the job has no session yet or isn't in a state
        where messaging makes sense.
        """
        job = self._store.load_job(job_id)
        if not job or not job.session_id:
            return False
        if job.state not in (JobState.RUNNING, JobState.COMPLETED, JobState.PAUSED_DUE_TO_LIMIT):
            return False
        asyncio.ensure_future(
            send_message(job, message, self._store, self._broadcast)
        )
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

    async def _handle_stop(self, job: Job, stop_reason: StopReason) -> None:
        if stop_reason == StopReason.COMPLETED:
            job.transition(JobState.COMPLETED)
            self._store.save_job(job)
            await self._broadcast_state(job)

        elif stop_reason == StopReason.LIMIT_HIT:
            job.transition(JobState.PAUSED_DUE_TO_LIMIT)
            self._store.save_job(job)
            await self._broadcast_state(job)
            await self._wait_and_resume(job)

        else:
            job.transition(JobState.FAILED)
            self._store.save_job(job)
            await self._broadcast_state(job)

    async def _wait_and_resume(self, job: Job) -> None:
        retry_at = datetime.utcnow() + timedelta(seconds=LIMIT_RETRY_SECONDS)
        log.info("Job %s paused. Retrying at %s UTC", job.id, retry_at.strftime("%H:%M:%S"))

        await self._broadcast(job.id, {
            "type": "orch_status",
            "message": f"Rate limit hit. Retrying at {retry_at.strftime('%H:%M:%S UTC')}",
        })

        while datetime.utcnow() < retry_at:
            remaining = int((retry_at - datetime.utcnow()).total_seconds())
            await self._broadcast(job.id, {
                "type": "orch_status",
                "message": f"Waiting for limit reset… {remaining // 60}m remaining",
            })
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        job.resume_count += 1
        job.transition(JobState.RUNNING)
        self._store.save_job(job)
        await self._broadcast_state(job)

        stop_reason = await run_job(job, self._store, self._broadcast)
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

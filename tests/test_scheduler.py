"""Integration tests for Scheduler queue and lifecycle."""

from __future__ import annotations

import asyncio
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator.job import Job, JobState
from orchestrator.scheduler import Scheduler
from orchestrator.store import Store
from orchestrator.detector import StopReason


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return Store(db_path=tmp_path / "test.db")


@pytest.fixture
def broadcast_log():
    """Collects all broadcast calls for inspection."""
    log = []
    async def broadcast(job_id, event):
        log.append((job_id, event))
    return broadcast, log


@pytest.fixture
def scheduler(store, broadcast_log):
    broadcast, _ = broadcast_log
    return Scheduler(store=store, broadcast=broadcast)


# ── Queue ordering ────────────────────────────────────────────────────────────

class TestQueue:
    @pytest.mark.asyncio
    async def test_single_job_runs_immediately(self, scheduler, store):
        job = Job(prompt="test")

        with patch("orchestrator.scheduler.run_job", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = StopReason.COMPLETED
            scheduler.enqueue_job(job)
            await asyncio.sleep(0.05)

        loaded = store.load_job(job.id)
        assert loaded.state == JobState.COMPLETED

    @pytest.mark.asyncio
    async def test_second_job_waits_while_first_runs(self, scheduler, store):
        job1 = Job(prompt="first")
        job2 = Job(prompt="second")

        run_order = []
        first_started = asyncio.Event()
        first_release = asyncio.Event()

        async def fake_run(job, *args, **kwargs):
            run_order.append(job.id)
            if job.id == job1.id:
                first_started.set()
                await first_release.wait()
            return StopReason.COMPLETED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            scheduler.enqueue_job(job1)
            await first_started.wait()

            # job2 enqueued while job1 is running
            scheduler.enqueue_job(job2)
            await asyncio.sleep(0.05)

            # job2 should still be queued
            loaded2 = store.load_job(job2.id)
            assert loaded2.state == JobState.QUEUED

            # Release job1
            first_release.set()
            await asyncio.sleep(0.05)

        # Both should have run in order
        assert run_order == [job1.id, job2.id]

    @pytest.mark.asyncio
    async def test_queue_status_reflects_waiting_jobs(self, scheduler, store):
        job1 = Job(prompt="first")
        job2 = Job(prompt="second")
        job3 = Job(prompt="third")

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(job, *args, **kwargs):
            if job.id == job1.id:
                started.set()
                await release.wait()
            return StopReason.COMPLETED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            scheduler.enqueue_job(job1)
            await started.wait()
            scheduler.enqueue_job(job2)
            scheduler.enqueue_job(job3)
            await asyncio.sleep(0.05)

            status = scheduler.queue_status()
            assert len(status) == 2
            assert status[0]["position"] == 1
            assert status[1]["position"] == 2
            assert status[0]["job_id"] == job2.id
            assert status[1]["job_id"] == job3.id

            release.set()
            await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_cancel_queued_job_removes_from_queue(self, scheduler, store):
        job1 = Job(prompt="first")
        job2 = Job(prompt="second")

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(job, *args, **kwargs):
            if job.id == job1.id:
                started.set()
                await release.wait()
            return StopReason.COMPLETED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            scheduler.enqueue_job(job1)
            await started.wait()
            scheduler.enqueue_job(job2)
            await asyncio.sleep(0.02)

            cancelled = scheduler.cancel_job(job2.id)
            assert cancelled is True
            assert job2.id not in [item["job_id"] for item in scheduler.queue_status()]

            loaded2 = store.load_job(job2.id)
            assert loaded2.state == JobState.FAILED

            release.set()
            await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_next_job_starts_after_completion(self, scheduler, store):
        job1 = Job(prompt="first")
        job2 = Job(prompt="second")

        release = asyncio.Event()
        completed = asyncio.Event()

        async def fake_run(job, *args, **kwargs):
            if job.id == job1.id:
                await release.wait()
            else:
                completed.set()
            return StopReason.COMPLETED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            scheduler.enqueue_job(job1)
            scheduler.enqueue_job(job2)
            await asyncio.sleep(0.02)
            release.set()
            await asyncio.wait_for(completed.wait(), timeout=2.0)

        assert store.load_job(job2.id).state == JobState.COMPLETED


# ── Limit handling ────────────────────────────────────────────────────────────

class TestLimitHandling:
    @pytest.mark.asyncio
    async def test_limit_hit_transitions_to_paused(self, scheduler, store):
        job = Job(prompt="test")
        resumed = asyncio.Event()
        call_count = 0

        async def fake_run(job, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return StopReason.LIMIT_HIT
            resumed.set()
            return StopReason.COMPLETED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            with patch("orchestrator.scheduler.LIMIT_RETRY_SECONDS", 0):
                with patch("orchestrator.scheduler.POLL_INTERVAL_SECONDS", 0):
                    scheduler.enqueue_job(job)
                    await asyncio.wait_for(resumed.wait(), timeout=3.0)

        loaded = store.load_job(job.id)
        assert loaded.state == JobState.COMPLETED
        assert loaded.resume_count == 1

    @pytest.mark.asyncio
    async def test_failed_job_stops_not_retried(self, scheduler, store):
        job = Job(prompt="test")

        async def fake_run(job, *args, **kwargs):
            return StopReason.FAILED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            scheduler.enqueue_job(job)
            await asyncio.sleep(0.1)

        loaded = store.load_job(job.id)
        assert loaded.state == JobState.FAILED


# ── Broadcast events ──────────────────────────────────────────────────────────

class TestBroadcast:
    @pytest.mark.asyncio
    async def test_orch_state_events_broadcast_on_transitions(self, store, broadcast_log):
        broadcast, log = broadcast_log
        sched = Scheduler(store=store, broadcast=broadcast)
        job = Job(prompt="test")

        async def fake_run(job, *args, **kwargs):
            return StopReason.COMPLETED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            sched.enqueue_job(job)
            await asyncio.sleep(0.1)

        state_events = [e for _, e in log if e.get("type") == "orch_state"]
        states = [e["state"] for e in state_events]
        assert "running" in states
        assert "completed" in states

    @pytest.mark.asyncio
    async def test_cancel_running_job_broadcasts_failed(self, store, broadcast_log):
        broadcast, log = broadcast_log
        sched = Scheduler(store=store, broadcast=broadcast)
        job = Job(prompt="test")

        started = asyncio.Event()

        async def fake_run(job, *args, **kwargs):
            started.set()
            await asyncio.sleep(10)
            return StopReason.COMPLETED

        with patch("orchestrator.scheduler.run_job", side_effect=fake_run):
            sched.enqueue_job(job)
            await started.wait()
            sched.cancel_job(job.id)
            await asyncio.sleep(0.1)

        state_events = [e for _, e in log if e.get("type") == "orch_state"]
        states = [e["state"] for e in state_events]
        assert "failed" in states

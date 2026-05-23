"""Tests for SQLite persistence."""

import tempfile
from pathlib import Path

import pytest
from orchestrator.job import Job, JobState
from orchestrator.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(db_path=tmp_path / "test.db")


class TestJobPersistence:
    def test_save_and_load(self, store):
        job = Job(prompt="fix the bug")
        store.save_job(job)
        loaded = store.load_job(job.id)
        assert loaded is not None
        assert loaded.id == job.id
        assert loaded.prompt == "fix the bug"
        assert loaded.state == JobState.QUEUED

    def test_load_nonexistent(self, store):
        assert store.load_job("nonexistent-id") is None

    def test_update_job(self, store):
        job = Job(prompt="refactor module")
        store.save_job(job)

        job.transition(JobState.RUNNING)
        store.save_job(job)

        loaded = store.load_job(job.id)
        assert loaded.state == JobState.RUNNING

    def test_list_jobs(self, store):
        j1 = Job(prompt="task 1")
        j2 = Job(prompt="task 2")
        store.save_job(j1)
        store.save_job(j2)

        jobs = store.list_jobs()
        assert len(jobs) == 2

    def test_list_jobs_by_state(self, store):
        j1 = Job(prompt="running job")
        j1.transition(JobState.RUNNING)
        store.save_job(j1)

        j2 = Job(prompt="queued job")
        store.save_job(j2)

        running = store.list_jobs_by_state(JobState.RUNNING)
        assert len(running) == 1
        assert running[0].id == j1.id

    def test_session_id_roundtrip(self, store):
        job = Job(prompt="test")
        job.session_id = "abc-123"
        store.save_job(job)
        loaded = store.load_job(job.id)
        assert loaded.session_id == "abc-123"


class TestLogPersistence:
    def test_append_and_retrieve(self, store):
        job = Job(prompt="test")
        store.save_job(job)
        store.append_log(job.id, 1, "raw", "hello world")
        store.append_log(job.id, 2, "result", '{"type":"result"}')

        logs = store.get_logs(job.id)
        assert len(logs) == 2
        assert logs[0]["raw"] == "hello world"
        assert logs[1]["event_type"] == "result"

    def test_after_seq_filter(self, store):
        job = Job(prompt="test")
        store.save_job(job)
        for i in range(1, 6):
            store.append_log(job.id, i, "raw", f"line {i}")

        logs = store.get_logs(job.id, after_seq=3)
        assert len(logs) == 2
        assert logs[0]["seq"] == 4

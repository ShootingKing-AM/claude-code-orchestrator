"""Tests for Job state machine."""

import pytest
from orchestrator.job import Job, JobState


class TestJobStateMachine:
    def test_initial_state(self):
        job = Job(prompt="test")
        assert job.state == JobState.QUEUED

    def test_valid_transition_queued_to_running(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        assert job.state == JobState.RUNNING

    def test_valid_transition_running_to_completed(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        job.transition(JobState.COMPLETED)
        assert job.state == JobState.COMPLETED

    def test_valid_transition_running_to_paused(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        job.transition(JobState.PAUSED_DUE_TO_LIMIT)
        assert job.state == JobState.PAUSED_DUE_TO_LIMIT

    def test_valid_transition_paused_to_running(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        job.transition(JobState.PAUSED_DUE_TO_LIMIT)
        job.transition(JobState.RUNNING)
        assert job.state == JobState.RUNNING

    def test_invalid_transition_completed_to_running(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        job.transition(JobState.COMPLETED)
        with pytest.raises(ValueError):
            job.transition(JobState.RUNNING)

    def test_invalid_transition_queued_to_completed(self):
        job = Job(prompt="test")
        with pytest.raises(ValueError):
            job.transition(JobState.COMPLETED)

    def test_is_terminal_completed(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        job.transition(JobState.COMPLETED)
        assert job.is_terminal()

    def test_is_terminal_failed(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        job.transition(JobState.FAILED)
        assert job.is_terminal()

    def test_is_not_terminal_running(self):
        job = Job(prompt="test")
        job.transition(JobState.RUNNING)
        assert not job.is_terminal()

    def test_to_dict_from_dict_roundtrip(self):
        job = Job(prompt="build a feature", working_dir="/home/user/proj")
        job.transition(JobState.RUNNING)
        job.session_id = "sess-abc"
        job.resume_count = 2

        d = job.to_dict()
        loaded = Job.from_dict(d)

        assert loaded.id == job.id
        assert loaded.prompt == job.prompt
        assert loaded.state == job.state
        assert loaded.session_id == job.session_id
        assert loaded.resume_count == job.resume_count
        assert loaded.working_dir == job.working_dir

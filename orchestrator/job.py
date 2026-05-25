"""Job dataclass and state machine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_DUE_TO_LIMIT = "paused_due_to_limit"
    FAILED = "failed"
    COMPLETED = "completed"


TERMINAL_STATES = {JobState.FAILED, JobState.COMPLETED}
VALID_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.RUNNING, JobState.FAILED},   # FAILED = cancelled while queued
    JobState.RUNNING: {JobState.COMPLETED, JobState.PAUSED_DUE_TO_LIMIT, JobState.FAILED},
    JobState.PAUSED_DUE_TO_LIMIT: {JobState.RUNNING},
    JobState.FAILED: set(),
    JobState.COMPLETED: set(),
}


@dataclass
class Job:
    prompt: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: JobState = JobState.QUEUED
    session_id: Optional[str] = None        # claude --resume <id>
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    error: Optional[str] = None
    resume_count: int = 0
    working_dir: Optional[str] = None       # cwd passed to claude
    title: Optional[str] = None             # user-editable display name
    model: Optional[str] = None             # --model flag (None = claude default)
    max_turns: Optional[int] = None         # --max-turns flag (None = unlimited)
    effort: Optional[str] = None            # --effort flag (low/medium/high/xhigh/max)
    input_tokens: int = 0                   # uncached input tokens (usually tiny)
    output_tokens: int = 0
    cache_read_tokens: int = 0              # input tokens served from prompt cache
    cache_creation_tokens: int = 0          # input tokens written to prompt cache
    total_cost_usd: float = 0.0             # cumulative USD cost across all runs

    def transition(self, new_state: JobState) -> None:
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(f"Invalid transition {self.state} → {new_state}")
        self.state = new_state
        self.updated_at = datetime.utcnow().isoformat()

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "state": self.state.value,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "resume_count": self.resume_count,
            "working_dir": self.working_dir,
            "title": self.title,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_cost_usd": self.total_cost_usd,
            "model": self.model,
            "max_turns": self.max_turns,
            "effort": self.effort,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        j = cls(prompt=d["prompt"], id=d["id"])
        j.state = JobState(d["state"])
        j.session_id = d.get("session_id")
        j.created_at = d["created_at"]
        j.updated_at = d["updated_at"]
        j.error = d.get("error")
        j.resume_count = d.get("resume_count", 0)
        j.working_dir = d.get("working_dir")
        j.title = d.get("title")
        j.input_tokens = d.get("input_tokens", 0)
        j.output_tokens = d.get("output_tokens", 0)
        j.cache_read_tokens = d.get("cache_read_tokens", 0)
        j.cache_creation_tokens = d.get("cache_creation_tokens", 0)
        j.total_cost_usd = d.get("total_cost_usd", 0.0)
        j.model = d.get("model")
        j.max_turns = d.get("max_turns")
        j.effort = d.get("effort")
        return j

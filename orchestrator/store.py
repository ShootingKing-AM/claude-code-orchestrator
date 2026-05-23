"""SQLite-backed persistence for jobs and log lines."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

from .job import Job, JobState

DEFAULT_DB = Path.home() / ".orch" / "orch.db"


class Store:
    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id          TEXT PRIMARY KEY,
                    data        TEXT NOT NULL,
                    state       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      TEXT NOT NULL REFERENCES jobs(id),
                    seq         INTEGER NOT NULL,
                    event_type  TEXT NOT NULL,
                    raw         TEXT NOT NULL,
                    ts          TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_logs_job ON logs(job_id, seq);
            """)

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def save_job(self, job: Job) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(id, data, state, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    data=excluded.data,
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (job.id, json.dumps(job.to_dict()), job.state.value, job.updated_at),
            )

    def load_job(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return Job.from_dict(json.loads(row["data"]))

    def list_jobs(self) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM jobs ORDER BY updated_at DESC"
            ).fetchall()
        return [Job.from_dict(json.loads(r["data"])) for r in rows]

    def last_seq_by_job(self) -> dict[str, int]:
        """Return {job_id: max_seq} for all jobs that have log entries."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id, MAX(seq) as last_seq FROM logs GROUP BY job_id"
            ).fetchall()
        return {r["job_id"]: r["last_seq"] for r in rows}

    def list_jobs_by_state(self, state: JobState) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM jobs WHERE state=? ORDER BY updated_at DESC",
                (state.value,),
            ).fetchall()
        return [Job.from_dict(json.loads(r["data"])) for r in rows]

    # ── Logs ──────────────────────────────────────────────────────────────────

    def append_log(self, job_id: str, seq: int, event_type: str, raw: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO logs(job_id, seq, event_type, raw) VALUES(?,?,?,?)",
                (job_id, seq, event_type, raw),
            )

    def get_logs(self, job_id: str, after_seq: int = -1) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, event_type, raw, ts FROM logs WHERE job_id=? AND seq>? ORDER BY seq",
                (job_id, after_seq),
            ).fetchall()
        return [dict(r) for r in rows]

    def iter_logs(self, job_id: str) -> Iterator[dict]:
        yield from self.get_logs(job_id)

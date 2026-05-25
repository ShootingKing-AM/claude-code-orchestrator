# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A local web-based supervisor that runs `claude` CLI as a subprocess, streams its JSON output to a browser in real time, and automatically resumes jobs when Claude's usage limit resets. The key problem it solves: distinguishing "Claude stopped because the task is done" from "Claude stopped because the rate limit hit", then waiting and retrying the latter automatically.

## Commands

```bash
# Run the server (from repo root)
python3 -m web.server
# → opens at http://localhost:7842

# Run all tests
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_detector.py -v

# Run a single test by name
python3 -m pytest tests/test_detector.py::TestIsLimitMessage::test_rate_limit -v

# Install dependencies (pip bootstrapped to ~/.local/bin/)
~/.local/bin/pip install -r requirements.txt
~/.local/bin/pip install pytest
```

Python 3.10 is the runtime (`/usr/bin/python3`). `pip` is not on PATH — use `~/.local/bin/pip` or `python3 -m pip` after bootstrapping.

## Architecture

The data flow for a job is:

```
POST /api/jobs
  → Scheduler.start_job()
    → asyncio.Task: _lifecycle()
      → runner.run_job()          # spawns: claude --output-format stream-json --print --message <prompt>
        → reads stdout line by line
          → detector.classify_result_event() / classify_line()  # is this completion or limit?
          → store.append_log()    # persists every raw line to SQLite
          → broadcast()           # pushes to all SSE queues for this job
      → StopReason returned
    → Scheduler._handle_stop()
      → COMPLETED → job.transition(COMPLETED)
      → LIMIT_HIT → job.transition(PAUSED_DUE_TO_LIMIT) → _wait_and_resume() → re-runs run_job()
      → FAILED    → job.transition(FAILED)

GET /api/jobs/{id}/stream (SSE)
  → replays stored logs from SQLite, then tails live asyncio.Queue events
```

### Key design decisions

**Stop reason detection has two layers** ([orchestrator/detector.py](orchestrator/detector.py)):
1. Primary: parse the `result` JSON event's `subtype` field (`success` → COMPLETED, error subtypes with limit phrases → LIMIT_HIT).
2. Fallback: scan every raw line for limit-phrase regex patterns — catches cases where Claude prints a plain-text limit message outside a structured event.

**Session resume**: On first run, `runner.py` captures `session_id` from the `system`/`init` event emitted by `claude`. On resume, it invokes `claude --resume <session_id> --message continue`. The session ID is persisted in the `Job` record in SQLite so it survives server restarts.

**SSE fan-out**: `web/server.py` maintains `_subscribers: dict[job_id → list[asyncio.Queue]]`. Each SSE connection gets its own queue. `broadcast()` pushes to all queues for a job. On connection the endpoint first replays historical log rows from SQLite (`after_seq` param), then tails the live queue — so browser refreshes don't lose history.

**State machine** is enforced by `Job.transition()` — invalid transitions raise `ValueError`. Valid graph: `queued → running → {completed, paused_due_to_limit, failed}`, `paused_due_to_limit → running`.

**Store** ([orchestrator/store.py](orchestrator/store.py)): SQLite at `~/.orch/orch.db` with WAL mode. Jobs are stored as JSON blobs (`data` column) plus a denormalized `state` column for fast `WHERE state=?` queries. Logs are append-only rows with a monotonic `seq` per job.

**Timing constants** to adjust in [orchestrator/scheduler.py](orchestrator/scheduler.py):
- `LIMIT_RETRY_SECONDS = 3600` — how long to wait before resuming after a limit hit
- `POLL_INTERVAL_SECONDS = 300` — how often to emit "X minutes remaining" status while waiting

## Synthetic event types

The frontend renders both real Claude events and two orchestrator-injected types:
- `orch_state` — emitted whenever job state transitions; frontend updates the badge
- `orch_status` — human-readable status messages (e.g. "Waiting for limit reset… 47m remaining")

These are never persisted to the `logs` table — they're broadcast-only.

## Adding new limit patterns

Edit `LIMIT_PATTERNS` in [orchestrator/detector.py](orchestrator/detector.py). Each entry is a compiled regex tested against error messages and raw output lines. The `capacity` and `529` patterns are intentionally broad — tighten them if they cause false positives.

## Frontend rendering

[web/static/app.js](web/static/app.js) handles event rendering in `renderEvent()`. Each `type` value maps to a styled block. Tool calls (`tool_use`) get a teal left-border block; tool results get purple; `orch_status` gets amber. To add rendering for a new event type, add a `case` in the `switch` inside `renderEvent()`.

# When you are fixing Bugs or Issues !! VERY IMPORTANT !!
1. Replicate the bug by writing a End to end integration playwright based test. Conduct the Test and get the screenshots of bugs.
2. Investigate and solve the bug, by going to the root cause.
3. Run the Test using playwright, take a screenshot, check the screenshot.
4. Till the bug is fixed via testing the screenshot-playwright test, keep fixing the bug
5. Once the bug is fixed, then commit and stop the bug fix process.
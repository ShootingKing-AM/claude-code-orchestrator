# Claude Code Orchestrator

A lightweight web-based supervisor that runs Claude Code as a worker, streams output live to a browser UI, and automatically resumes jobs when usage limits reset.

## How it works

```
Browser UI  ←──SSE──  FastAPI server  ──→  Scheduler  ──→  claude CLI
                            │                                    │
                            └──────────  SQLite DB  ────────────┘
```

1. You submit a task via the web UI.
2. The server spawns `claude --output-format stream-json --print` as a subprocess.
3. Events stream in real time to your browser (tool calls, text, results).
4. If Claude stops with a rate-limit message, the job is marked **paused_due_to_limit** and the scheduler waits ~60 minutes before automatically resuming with `--resume <session-id>`.
5. If Claude completes normally, the job is marked **completed**.
6. All events are stored in SQLite so you can reload and replay logs at any time.

## Requirements

- Python 3.11+
- `claude` CLI installed and authenticated (`claude --version` should work)
- Claude Pro or Max subscription

## Install

```bash
cd /path/to/orch
pip install -r requirements.txt
```

## Run

```bash
python -m web.server
```

Then open **http://localhost:9000** in your browser.

## Usage

1. Click **+ New Job**
2. Enter your task prompt (e.g. "Refactor the auth module to use JWT")
3. Optionally set the working directory (the repo root Claude should operate in)
4. Click **Start Job**

Claude's output streams in real time. Tool calls are highlighted in teal, tool results in purple, orchestrator status messages in amber.

If a rate limit is hit, you'll see an amber "Waiting for limit reset…" message with a countdown. The job resumes automatically — no action needed.

## State machine

```
queued → running → completed
                 → paused_due_to_limit → running (auto-resume after ~60 min)
                 → failed
```

## Configuration

Edit `orchestrator/scheduler.py` to adjust timing:

```python
LIMIT_RETRY_SECONDS = 60 * 60   # wait 60 min before retry (default)
POLL_INTERVAL_SECONDS = 5 * 60  # status update every 5 min while waiting
```

## Run tests

```bash
pip install pytest
pytest tests/ -v
```

## Project structure

```
orch/
├── orchestrator/
│   ├── job.py          # Job dataclass + state machine
│   ├── detector.py     # Parse Claude events → stop reason
│   ├── store.py        # SQLite persistence
│   ├── runner.py       # Spawn claude CLI, stream JSON events
│   └── scheduler.py    # Retry/resume loop
├── web/
│   ├── server.py       # FastAPI + SSE endpoints
│   └── static/
│       ├── index.html
│       ├── style.css
│       └── app.js
├── tests/
│   ├── test_job.py
│   ├── test_detector.py
│   └── test_store.py
├── requirements.txt
└── README.md
```

## Assumptions and edge cases

- **Session resume**: Claude Code supports `--resume <session-id>` to continue a prior conversation. The orchestrator captures the session ID from the first `system`/`init` event and uses it on resume.
- **Limit detection**: The orchestrator looks for common limit phrases in Claude's output (`usage limit`, `rate limit`, `try again later`, etc.). If Anthropic changes the exact wording, add patterns to `orchestrator/detector.py`.
- **Retry window**: Claude Pro limits reset on a rolling window. 60 minutes is a safe default. Adjust `LIMIT_RETRY_SECONDS` if your account resets faster or slower.
- **Single machine**: This is a local supervisor, not a distributed system. All state lives in `~/.orch/orch.db`.
- **One job at a time per machine**: Multiple jobs can run concurrently (each gets its own process), but Claude Code itself serializes tool use within each session.
- **Non-interactive mode**: Uses `--print` flag so Claude runs without prompting for input. Not all Claude Code features work in non-interactive mode.

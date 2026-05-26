"""GitHub Copilot runner — uses api.githubcopilot.com (OpenAI-compatible)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING

from .detector import StopReason
from .job import Job, JobState
from .store import Store

log = logging.getLogger(__name__)

_COPILOT_URL = "https://api.githubcopilot.com/chat/completions"
_COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
_DEFAULT_MODEL = "claude-sonnet-4.6"

# Required headers for GitHub Copilot API
_COPILOT_HEADERS_BASE = {
    "Editor-Version": "vscode/1.96.0",
    "Copilot-Integration-Id": "vscode-chat",
    "Content-Type": "application/json",
}

_MAX_HISTORY_MESSAGES = 80
_MAX_HISTORY_CHARS = 120_000
_AGENT_MAX_MESSAGES = 120
_AGENT_MAX_CHARS = 180_000
_AGENT_TOOL_TIMEOUT_SECONDS = 180


def _get_token() -> str:
    """
    Return a GitHub OAuth token suitable for the Copilot API.
    Priority:
      1. COPILOT_TOKEN env var  (OAuth token, gho_...)
      2. GITHUB_TOKEN env var   (if it looks like an OAuth token gho_...)
      3. `gh auth token` CLI fallback
    PATs (ghp_...) are NOT accepted by api.githubcopilot.com.
    """
    for var in ("COPILOT_TOKEN", "GITHUB_TOKEN"):
        tok = os.environ.get(var, "").strip()
        if tok and not tok.startswith("ghp_"):
            return tok  # looks like a valid OAuth / app token

    # Fallback: ask gh CLI — must exclude GITHUB_TOKEN from env so gh
    # uses its own stored OAuth token, not the PAT from the environment.
    try:
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        result = subprocess.run(
            ["gh", "auth", "status", "--show-token"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        for line in result.stdout.splitlines() + result.stderr.splitlines():
            if "Token:" in line:
                tok = line.split("Token:")[-1].strip()
                if tok and not tok.startswith("ghp_"):
                    return tok
    except Exception:
        pass

    raise RuntimeError(
        "No valid GitHub OAuth token found for the Copilot API.\n"
        "Set COPILOT_TOKEN=gho_... in .env (get it via: gh auth status --show-token)\n"
        "PATs (ghp_...) are not supported by api.githubcopilot.com."
    )


def _get_model(job: Job) -> str:
    return job.model or os.environ.get("COPILOT_MODEL", _DEFAULT_MODEL)


def _reconstruct_history(job: Job, store: Store) -> list[dict]:
    """
    Rebuild conversation history from stored logs for this job.
    Returns a list of {"role": ..., "content": ...} dicts suitable for the
    OpenAI-compatible chat completions API.
    """
    messages: list[dict] = [{"role": "user", "content": job.prompt}]

    if not job.session_id:
        return messages

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT event_type, raw FROM logs WHERE job_id=? ORDER BY seq",
            (job.id,),
        ).fetchall()

    # Accumulate consecutive assistant chunks into a single turn before
    # flushing, so the history strictly alternates user/assistant.
    current_assistant_parts: list[str] = []

    def _flush_assistant() -> None:
        if current_assistant_parts:
            text = "".join(current_assistant_parts)
            if text.strip():
                messages.append({"role": "assistant", "content": text})
            current_assistant_parts.clear()

    for event_type, raw in rows:
        if event_type == "user_msg":
            _flush_assistant()
            try:
                ev = json.loads(raw)
                text = ev.get("text", "")
                if text:
                    messages.append({"role": "user", "content": text})
            except (json.JSONDecodeError, AttributeError):
                pass

        elif event_type == "assistant":
            try:
                ev = json.loads(raw)
                content = (ev.get("message") or {}).get("content") or []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        current_assistant_parts.append(block.get("text", ""))
            except (json.JSONDecodeError, AttributeError):
                pass

        elif event_type == "result":
            # End of a run turn — flush any accumulated assistant text
            _flush_assistant()

    _flush_assistant()  # flush any leftover chunks

    return messages


def _content_len(value) -> int:
    """Rough character count for a message content payload."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        total = 0
        for item in value:
            if isinstance(item, dict):
                total += len(item.get("text", ""))
            elif isinstance(item, str):
                total += len(item)
            else:
                total += len(str(item))
        return total
    if isinstance(value, dict):
        return len(value.get("text", "")) or len(str(value))
    return len(str(value))


def _trim_messages_for_budget(
    messages: list[dict],
    *,
    max_messages: int,
    max_chars: int,
    keep_head: int = 0,
) -> list[dict]:
    """Trim oldest middle messages while preserving most recent context.

    Keeps the first `keep_head` messages (e.g., system prompt), then retains
    the newest messages within count and rough character budgets.
    """
    if len(messages) <= max_messages:
        total = sum(_content_len(m.get("content")) for m in messages)
        if total <= max_chars:
            return messages

    head = messages[:keep_head] if keep_head > 0 else []
    tail = messages[keep_head:] if keep_head > 0 else messages
    budget_msgs = max(0, max_messages - len(head))
    kept_tail = tail[-budget_msgs:] if budget_msgs > 0 else []

    while kept_tail:
        total_chars = sum(_content_len(m.get("content")) for m in head + kept_tail)
        if total_chars <= max_chars:
            break
        kept_tail = kept_tail[1:]

    return head + kept_tail


async def run_copilot_job(
    job: Job,
    store: Store,
    broadcast,
    message: str | None = None,
) -> tuple[StopReason, str]:
    """
    Run a job via GitHub Models API (OpenAI-compatible streaming).
    Signature mirrors runner.run_job for drop-in dispatch.

    Conversation history is always reconstructed from SQLite logs so that
    resume after rate-limit works correctly without server-side session state.
    The `message` parameter is accepted for API compatibility but is not
    appended here — callers that need to inject a new user turn should
    persist it to logs first (send_copilot_message does this).
    """
    try:
        import httpx
    except ImportError:
        raise ImportError(
            "httpx is required for the Copilot backend. "
            "Install it: pip install 'httpx[http2]>=0.27.0'"
        )

    token = _get_token()
    model = _get_model(job)

    # On first run, anchor the session to the job's own ID so the scheduler's
    # resume logic (which checks session_id) works for copilot jobs too.
    if not job.session_id:
        job.session_id = job.id
        store.save_job(job)

    # Reconstruct full conversation history from persisted logs
    history = _trim_messages_for_budget(
        _reconstruct_history(job, store),
        max_messages=_MAX_HISTORY_MESSAGES,
        max_chars=_MAX_HISTORY_CHARS,
        keep_head=1,
    )

    # Get starting seq (continue monotonically after existing rows)
    with store._connect() as conn:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM logs WHERE job_id=?", (job.id,)
        ).fetchone()[0]

    stop_reason = StopReason.UNKNOWN
    accumulated_text = ""
    last_result_text = ""
    now_iso = datetime.utcnow().isoformat

    headers = {
        **_COPILOT_HEADERS_BASE,
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }
    payload: dict = {
        "model": model,
        "messages": history,
        "stream": True,
    }

    log.info(
        "Starting copilot job %s model=%s history_turns=%d",
        job.id, model, len(history),
    )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0)
        ) as client:
            async with client.stream(
                "POST",
                _COPILOT_URL,
                headers=headers,
                json=payload,
            ) as response:

                # ── Rate-limit / service-unavailable ──────────────────────────
                if response.status_code in (429, 529):
                    err_body = await response.aread()
                    err_text = err_body.decode("utf-8", errors="replace")
                    log.warning(
                        "Copilot rate limited (HTTP %d) for job %s: %s",
                        response.status_code, job.id, err_text[:200],
                    )
                    seq += 1
                    result_event: dict = {
                        "type": "result",
                        "subtype": "error",
                        "error": "rate limited",
                        "api_error_status": response.status_code,
                    }
                    raw_line = json.dumps(result_event)
                    store.append_log(job.id, seq, "result", raw_line)
                    await broadcast(
                        job.id,
                        {**result_event, "seq": seq, "ts": now_iso(), "raw": raw_line},
                    )
                    return StopReason.LIMIT_HIT, err_text

                # ── Other HTTP errors ─────────────────────────────────────────
                if response.status_code >= 400:
                    err_body = await response.aread()
                    err_text = err_body.decode("utf-8", errors="replace")
                    log.error(
                        "Copilot API error HTTP %d for job %s: %s",
                        response.status_code, job.id, err_text[:500],
                    )
                    seq += 1
                    result_event = {
                        "type": "result",
                        "subtype": "error",
                        "error": f"HTTP {response.status_code}: {err_text[:200]}",
                    }
                    raw_line = json.dumps(result_event)
                    store.append_log(job.id, seq, "result", raw_line)
                    await broadcast(
                        job.id,
                        {**result_event, "seq": seq, "ts": now_iso(), "raw": raw_line},
                    )
                    return StopReason.FAILED, err_text

                # ── Stream SSE lines ──────────────────────────────────────────
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line == "[DONE]":
                        break
                    if not line.startswith("{"):
                        continue

                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    for choice in choices:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            accumulated_text += content
                            seq += 1
                            assistant_event: dict = {
                                "type": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": content}]
                                },
                            }
                            raw_line_out = json.dumps(assistant_event)
                            store.append_log(job.id, seq, "assistant", raw_line_out)
                            await broadcast(
                                job.id,
                                {
                                    **assistant_event,
                                    "seq": seq,
                                    "ts": now_iso(),
                                    "raw": raw_line_out,
                                },
                            )

    except httpx.TimeoutException as exc:
        log.error("Copilot job %s timed out: %s", job.id, exc)
        seq += 1
        result_event = {
            "type": "result",
            "subtype": "error",
            "error": f"timeout: {exc}",
        }
        raw_line = json.dumps(result_event)
        store.append_log(job.id, seq, "result", raw_line)
        await broadcast(
            job.id, {**result_event, "seq": seq, "ts": now_iso(), "raw": raw_line}
        )
        return StopReason.FAILED, str(exc)

    # ── Emit final success result event ───────────────────────────────────────
    last_result_text = accumulated_text
    seq += 1
    result_event = {
        "type": "result",
        "subtype": "success",
        "result": accumulated_text,
    }
    raw_line_out = json.dumps(result_event)
    store.append_log(job.id, seq, "result", raw_line_out)
    await broadcast(
        job.id,
        {**result_event, "seq": seq, "ts": now_iso(), "raw": raw_line_out},
    )
    stop_reason = StopReason.COMPLETED

    log.info(
        "Copilot job %s completed: %d chars generated", job.id, len(accumulated_text)
    )
    return stop_reason, last_result_text


async def send_copilot_message(
    job: Job,
    message: str,
    store: Store,
    broadcast,
) -> StopReason:
    """
    Send a follow-up message into a copilot job session.
    Mirrors runner.send_message — persists the user turn to logs then
    calls run_copilot_job which reconstructs history from those logs.
    """
    if not job.session_id:
        raise ValueError(
            "Job has no session_id — cannot send message before session starts"
        )

    # Persist user message so it appears in history on replay and reconstruction
    user_raw = json.dumps({
        "type": "user_msg",
        "text": message,
        "ts": datetime.utcnow().isoformat(),
    })
    with store._connect() as conn:
        last = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM logs WHERE job_id=?", (job.id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO logs(job_id, seq, event_type, raw) VALUES(?,?,?,?)",
            (job.id, last + 1, "user_msg", user_raw),
        )
    await broadcast(job.id, {"type": "user_msg", "text": message, "seq": last + 1})

    # message=None because it's already in the logs — _reconstruct_history will pick it up
    stop_reason, _ = await run_copilot_job(job, store, broadcast, message=None)
    return stop_reason


# ── Copilot Agent (tool-augmented agentic loop) ───────────────────────────────

_AGENT_MAX_TURNS = 30  # hard cap on tool-call rounds to prevent infinite loops
_AGENT_RETRY_ATTEMPTS = 3
_AGENT_RETRY_BASE_SECONDS = 1.0

_AGENT_SYSTEM_PROMPT = """\
You are a coding agent with direct tool access. Use the provided tools to \
accomplish tasks — read files, write files, run shell commands, search code, \
list directories. Always use tools rather than describing what you would do.

After making changes verify they work (run tests, read the modified file, etc.). \
When the task is fully complete, give a brief summary of what you did.\
"""


def _is_retryable_status(status_code: int) -> bool:
    """Return True for transient HTTP statuses that merit retry."""
    return status_code in {408, 409, 425, 500, 502, 503, 504}


def _retry_delay_seconds(attempt: int) -> float:
    """Exponential backoff delay for retry attempt index (0-based)."""
    return _AGENT_RETRY_BASE_SECONDS * (2 ** attempt)


def _parse_tool_arguments(raw_args: str) -> tuple[dict, str | None]:
    """Parse tool arguments robustly from streamed JSON fragments.

    Returns (args_dict, parse_error). If parsing fails, args_dict is empty and
    parse_error is a short diagnostic string.
    """
    text = (raw_args or "").strip()
    if not text:
        return {}, None

    # Common model behavior: wraps arguments in fenced JSON blocks.
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, None
        return {}, f"arguments must decode to object, got {type(parsed).__name__}"
    except json.JSONDecodeError:
        pass

    # Fallback for streams that append trailing non-JSON chars.
    try:
        decoder = json.JSONDecoder()
        parsed, end = decoder.raw_decode(text)
        if not isinstance(parsed, dict):
            return {}, f"arguments must decode to object, got {type(parsed).__name__}"
        trailing = text[end:].strip()
        if trailing:
            return parsed, f"ignored trailing characters after JSON object ({len(trailing)} chars)"
        return parsed, None
    except json.JSONDecodeError as exc:
        snippet = text[:160].replace("\n", " ")
        return {}, f"invalid JSON args: {exc.msg} near '{snippet}'"


def _is_cancelled_by_user(job_id: str, store: Store) -> bool:
    """Return True when scheduler/user has marked the job as cancelled."""
    latest = store.load_job(job_id)
    if not latest:
        return True
    if latest.state != JobState.FAILED:
        return False
    return (latest.error or "").lower().startswith("cancelled")


async def run_copilot_agent_job(
    job: Job,
    store: Store,
    broadcast,
    message: str | None = None,
) -> tuple[StopReason, str]:
    """
    Run a job via GitHub Copilot API with an agentic tool-use loop.
    Supports: read_file, write_file, str_replace_file, create_directory,
              list_directory, run_bash, search_files.
    """
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required: pip install 'httpx[http2]>=0.27.0'")

    from .tools import TOOL_SCHEMAS, execute_tool

    token = _get_token()
    model = _get_model(job)
    cwd = job.working_dir or str(__import__("pathlib").Path.home())

    # Anchor session_id so scheduler resume logic works
    if not job.session_id:
        job.session_id = job.id
        store.save_job(job)

    # Starting seq
    with store._connect() as conn:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM logs WHERE job_id=?", (job.id,)
        ).fetchone()[0]

    now_iso = datetime.utcnow().isoformat

    # Build initial messages: system + conversation history
    messages: list[dict] = [
        {"role": "system", "content": _AGENT_SYSTEM_PROMPT},
    ]
    # Reconstruct conversation history (prior runs if any)
    history = _trim_messages_for_budget(
        _reconstruct_history(job, store),
        max_messages=_MAX_HISTORY_MESSAGES,
        max_chars=_MAX_HISTORY_CHARS,
        keep_head=1,
    )
    # history[0] is always {"role": "user", "content": job.prompt}
    messages.extend(history)

    headers = {
        **_COPILOT_HEADERS_BASE,
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }

    accumulated_text = ""
    stop_reason = StopReason.UNKNOWN
    completed_turns = 0

    log.info(
        "Starting copilot-agent job %s model=%s cwd=%s",
        job.id, model, cwd,
    )

    async def _emit(seq_val: int, event: dict, event_type: str) -> None:
        """Persist + broadcast a single event."""
        raw = json.dumps(event)
        store.append_log(job.id, seq_val, event_type, raw)
        await broadcast(job.id, {**event, "seq": seq_val, "ts": now_iso(), "raw": raw})

    try:
        import httpx as _httpx

        async with _httpx.AsyncClient(
            timeout=_httpx.Timeout(300.0, connect=10.0)
        ) as client:

            for turn in range(_AGENT_MAX_TURNS):
                if _is_cancelled_by_user(job.id, store):
                    seq += 1
                    result_ev = {
                        "type": "result",
                        "subtype": "error",
                        "error": "cancelled by user",
                    }
                    await _emit(seq, result_ev, "result")
                    return StopReason.FAILED, "cancelled by user"

                messages = _trim_messages_for_budget(
                    messages,
                    max_messages=_AGENT_MAX_MESSAGES,
                    max_chars=_AGENT_MAX_CHARS,
                    keep_head=1,
                )

                payload: dict = {
                    "model": model,
                    "messages": messages,
                    "tools": TOOL_SCHEMAS,
                    "tool_choice": "auto",
                    "stream": True,
                }

                # ── Stream one API call ───────────────────────────────────────
                turn_text = ""
                tool_calls_acc: dict[int, dict] = {}  # index → {id, name, args}
                stream_ok = False
                last_stream_error = ""

                for attempt in range(_AGENT_RETRY_ATTEMPTS):
                    try:
                        async with client.stream(
                            "POST",
                            _COPILOT_URL,
                            headers=headers,
                            json=payload,
                        ) as response:

                            if response.status_code in (429, 529):
                                err_body = await response.aread()
                                err_text = err_body.decode("utf-8", errors="replace")
                                log.warning(
                                    "Copilot Agent rate limited (HTTP %d) job=%s: %s",
                                    response.status_code, job.id, err_text[:200],
                                )
                                seq += 1
                                result_ev = {
                                    "type": "result",
                                    "subtype": "error",
                                    "error": "rate limited",
                                    "api_error_status": response.status_code,
                                }
                                await _emit(seq, result_ev, "result")
                                return StopReason.LIMIT_HIT, err_text

                            if response.status_code >= 400:
                                err_body = await response.aread()
                                err_text = err_body.decode("utf-8", errors="replace")
                                if (
                                    _is_retryable_status(response.status_code)
                                    and attempt < _AGENT_RETRY_ATTEMPTS - 1
                                ):
                                    delay_s = _retry_delay_seconds(attempt)
                                    seq += 1
                                    await _emit(
                                        seq,
                                        {
                                            "type": "orch_status",
                                            "message": (
                                                f"Transient Copilot API error HTTP {response.status_code}; "
                                                f"retrying in {delay_s:.0f}s"
                                            ),
                                        },
                                        "orch_status",
                                    )
                                    await asyncio.sleep(delay_s)
                                    continue

                                log.error(
                                    "Copilot Agent API error HTTP %d job=%s: %s",
                                    response.status_code, job.id, err_text[:500],
                                )
                                seq += 1
                                result_ev = {
                                    "type": "result",
                                    "subtype": "error",
                                    "error": f"HTTP {response.status_code}: {err_text[:200]}",
                                }
                                await _emit(seq, result_ev, "result")
                                return StopReason.FAILED, err_text

                            async for line in response.aiter_lines():
                                line = line.strip()
                                if not line or line == "data: [DONE]":
                                    if line == "data: [DONE]":
                                        break
                                    continue
                                if line.startswith("data: "):
                                    line = line[6:]
                                if not line.startswith("{"):
                                    continue
                                try:
                                    chunk = json.loads(line)
                                except json.JSONDecodeError:
                                    continue

                                for choice in chunk.get("choices") or []:
                                    delta = choice.get("delta") or {}

                                    # Accumulate text content
                                    content = delta.get("content")
                                    if content:
                                        turn_text += content
                                        accumulated_text += content
                                        seq += 1
                                        asst_ev = {
                                            "type": "assistant",
                                            "message": {
                                                "content": [{"type": "text", "text": content}]
                                            },
                                        }
                                        await _emit(seq, asst_ev, "assistant")

                                    # Accumulate tool call fragments
                                    for tc_delta in delta.get("tool_calls") or []:
                                        idx = tc_delta.get("index", 0)
                                        if idx not in tool_calls_acc:
                                            tool_calls_acc[idx] = {"id": "", "name": "", "args": ""}
                                        acc = tool_calls_acc[idx]
                                        if tc_delta.get("id"):
                                            acc["id"] = tc_delta["id"]
                                        fn = tc_delta.get("function") or {}
                                        if fn.get("name"):
                                            acc["name"] = fn["name"]
                                        if fn.get("arguments"):
                                            acc["args"] += fn["arguments"]

                            stream_ok = True
                            break

                    except (_httpx.TimeoutException, _httpx.TransportError) as exc:
                        last_stream_error = str(exc)
                        if attempt < _AGENT_RETRY_ATTEMPTS - 1:
                            delay_s = _retry_delay_seconds(attempt)
                            seq += 1
                            await _emit(
                                seq,
                                {
                                    "type": "orch_status",
                                    "message": (
                                        "Transient network error while calling Copilot API; "
                                        f"retrying in {delay_s:.0f}s"
                                    ),
                                },
                                "orch_status",
                            )
                            await asyncio.sleep(delay_s)
                            continue
                        raise

                if not stream_ok:
                    seq += 1
                    result_ev = {
                        "type": "result",
                        "subtype": "error",
                        "error": f"failed to stream from Copilot API after retries: {last_stream_error}",
                    }
                    await _emit(seq, result_ev, "result")
                    return StopReason.FAILED, last_stream_error

                # ── Process tool calls or finish ──────────────────────────────
                if not tool_calls_acc:
                    # No tool calls → task complete
                    break

                # Build the assistant message with tool_calls for history
                asst_msg: dict = {"role": "assistant", "content": turn_text or ""}
                asst_msg["tool_calls"] = []
                tool_results_msgs: list[dict] = []

                for idx in sorted(tool_calls_acc.keys()):
                    tc = tool_calls_acc[idx]
                    call_id = tc["id"] or f"call_{job.id}_{turn}_{idx}"
                    tool_name = tc["name"]
                    tool_args, parse_error = _parse_tool_arguments(tc["args"])

                    asst_msg["tool_calls"].append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": tc["args"] or "{}",
                        },
                    })

                    # Emit tool_use event so frontend shows the teal block
                    seq += 1
                    tool_use_ev = {
                        "type": "tool_use",
                        "name": tool_name,
                        "input": tool_args,
                    }
                    if parse_error:
                        tool_use_ev["parse_warning"] = parse_error
                    await _emit(seq, tool_use_ev, "tool_use")

                    # Execute the tool
                    log.info(
                        "Agent job %s: executing tool %s args=%s",
                        job.id, tool_name, str(tool_args)[:200],
                    )
                    if not tool_name:
                        tool_result_text = "Error: tool call missing function name"
                    elif parse_error:
                        tool_result_text = f"Error: could not parse tool arguments for '{tool_name}': {parse_error}"
                    else:
                        try:
                            tool_result_text = await asyncio.wait_for(
                                asyncio.to_thread(execute_tool, tool_name, tool_args, cwd),
                                timeout=_AGENT_TOOL_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            tool_result_text = (
                                f"Error: tool '{tool_name}' exceeded timeout "
                                f"({_AGENT_TOOL_TIMEOUT_SECONDS}s)"
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # defensive: never crash whole run on tool failure
                            log.exception(
                                "Agent job %s: tool execution crashed tool=%s", job.id, tool_name
                            )
                            tool_result_text = f"Error: tool '{tool_name}' crashed: {exc}"

                    # Emit tool_result event so frontend shows the purple block
                    seq += 1
                    tool_result_ev = {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": tool_result_text,
                    }
                    await _emit(seq, tool_result_ev, "tool_result")

                    tool_results_msgs.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": tool_result_text,
                    })

                # Add assistant turn + tool results to history for next iteration
                messages.append(asst_msg)
                messages.extend(tool_results_msgs)
                completed_turns = turn + 1

            else:
                # Hit max turns — emit a warning result
                log.warning("Copilot Agent job %s hit max turns (%d)", job.id, _AGENT_MAX_TURNS)

    except _httpx.TimeoutException as exc:
        log.error("Copilot Agent job %s timed out: %s", job.id, exc)
        seq += 1
        result_ev = {"type": "result", "subtype": "error", "error": f"timeout: {exc}"}
        await _emit(seq, result_ev, "result")
        return StopReason.FAILED, str(exc)

    except Exception as exc:
        log.exception("Copilot Agent job %s unexpected error: %s", job.id, exc)
        seq += 1
        result_ev = {"type": "result", "subtype": "error", "error": str(exc)}
        await _emit(seq, result_ev, "result")
        return StopReason.FAILED, str(exc)

    # ── Emit success result ───────────────────────────────────────────────────
    seq += 1
    result_ev = {
        "type": "result",
        "subtype": "success",
        "result": accumulated_text,
    }
    await _emit(seq, result_ev, "result")
    stop_reason = StopReason.COMPLETED

    log.info(
        "Copilot Agent job %s completed: %d chars, %d tool turns",
        job.id, len(accumulated_text), completed_turns,
    )
    return stop_reason, accumulated_text

"""Parse Claude Code stream-json events to determine stop reason."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

# Patterns that indicate a usage/rate limit hit — used only as fallback
# on raw non-JSON lines or in error message fields.
# Do NOT match generic words that appear in normal output (e.g. "resets").
LIMIT_PATTERNS = [
    re.compile(r"usage.?limit", re.IGNORECASE),
    re.compile(r"session.?limit", re.IGNORECASE),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"quota exceeded", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"5.hour.limit", re.IGNORECASE),
    re.compile(r"daily.limit", re.IGNORECASE),
    re.compile(r"weekly.limit", re.IGNORECASE),
    re.compile(r"plan.limit", re.IGNORECASE),
    re.compile(r"HTTP 529", re.IGNORECASE),
]

# Primary: these HTTP status codes in api_error_status always mean limit hit
LIMIT_HTTP_CODES = {429, 529}

COMPLETION_SUBTYPES = {"success"}
ERROR_SUBTYPES = {"error_during_tool_use", "error"}

# Match "resets H:MM" or "resets HH:MM" optionally followed by am/pm and timezone
_RESET_TIME_RE = re.compile(
    r"resets?\s+(\d{1,2}):(\d{2})\s*(am|pm)?",
    re.IGNORECASE,
)


class StopReason(str, Enum):
    COMPLETED = "completed"
    LIMIT_HIT = "limit_hit"
    FAILED = "failed"
    UNKNOWN = "unknown"


def is_limit_message(text: str) -> bool:
    return any(p.search(text) for p in LIMIT_PATTERNS)


def parse_reset_time_from_event(event: dict) -> datetime | None:
    """
    Extract reset time from a rate_limit_event's resetsAt unix timestamp.
    This is unambiguous UTC — prefer over text parsing whenever available.
    """
    info = event.get("rate_limit_info") or {}
    resets_at = info.get("resetsAt")
    if resets_at:
        try:
            return datetime.utcfromtimestamp(int(resets_at))
        except (ValueError, TypeError, OSError):
            pass
    return None


def parse_reset_time(text: str) -> datetime | None:
    """
    Extract reset time from a limit message like
    "You've hit your session limit · resets 1:20pm (Asia/Kolkata)".

    IMPORTANT: Claude reports the time in the user's LOCAL timezone (e.g. IST),
    not the server's local time. We treat the time as IST (UTC+5:30) regardless
    of the server's timezone so the math is always correct.
    Returns a UTC datetime, or None if unparseable.
    """
    m = _RESET_TIME_RE.search(text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    meridiem = (m.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    _IST = timedelta(hours=5, minutes=30)
    now_utc = datetime.utcnow()
    now_ist = now_utc + _IST
    reset_ist = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset_ist <= now_ist:
        # Reset time already passed today — resume immediately
        return now_utc
    # Claude rate limits reset within ~1 hour. If the parsed time is more than
    # 2 hours away it means the limit message is from a previous session/day and
    # the reset already happened — resume immediately rather than waiting ~22h.
    if (reset_ist - now_ist) > timedelta(hours=2):
        return now_utc
    return reset_ist - _IST  # convert back to UTC


def _extract_result_text(event: dict[str, Any]) -> str:
    parts = []
    result = event.get("result", "")
    if isinstance(result, str):
        parts.append(result)
    content = event.get("content") or []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    return " ".join(parts)


def classify_result_event(event: dict[str, Any]) -> StopReason:
    """
    Classify a 'result' event from claude --output-format stream-json.

    Primary signal: api_error_status 429/529 → LIMIT_HIT
    Secondary: error message text contains limit phrases → LIMIT_HIT
    Otherwise: use subtype field.
    """
    # Primary: HTTP error code is the most reliable signal
    api_status = event.get("api_error_status")
    if api_status in LIMIT_HTTP_CODES:
        return StopReason.LIMIT_HIT

    subtype = event.get("subtype", "")
    error_info = event.get("error") or {}
    error_msg = ""
    if isinstance(error_info, dict):
        error_msg = error_info.get("message", "") or ""
    elif isinstance(error_info, str):
        error_msg = error_info

    top_msg = event.get("message", "") or ""

    # Secondary: explicit limit phrases in error fields only (not result text,
    # which can contain the word "limit" in normal conversation)
    if is_limit_message(error_msg) or is_limit_message(top_msg):
        return StopReason.LIMIT_HIT

    if subtype in COMPLETION_SUBTYPES:
        return StopReason.COMPLETED

    if subtype in ERROR_SUBTYPES:
        return StopReason.FAILED

    return StopReason.UNKNOWN


def classify_line(raw_line: str) -> StopReason | None:
    """Fallback scan of raw non-JSON lines for limit phrases."""
    if is_limit_message(raw_line):
        return StopReason.LIMIT_HIT
    return None


def extract_session_id(event: dict[str, Any]) -> str | None:
    """Pull the session_id from a system/init event if present."""
    return event.get("session_id") or event.get("sessionId")

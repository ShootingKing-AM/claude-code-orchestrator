"""Parse Claude Code stream-json events to determine stop reason."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

# Patterns that indicate a usage/rate limit hit
LIMIT_PATTERNS = [
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"quota exceeded", re.IGNORECASE),
    re.compile(r"try again later", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"capacity", re.IGNORECASE),
    re.compile(r"5.hour.limit", re.IGNORECASE),
    re.compile(r"daily.limit", re.IGNORECASE),
    re.compile(r"weekly.limit", re.IGNORECASE),
    re.compile(r"plan.limit", re.IGNORECASE),
    re.compile(r"529", re.IGNORECASE),          # HTTP 529 overloaded
]

COMPLETION_SUBTYPES = {"success"}
ERROR_SUBTYPES = {"error_during_tool_use", "error"}


class StopReason(str, Enum):
    COMPLETED = "completed"
    LIMIT_HIT = "limit_hit"
    FAILED = "failed"
    UNKNOWN = "unknown"


def is_limit_message(text: str) -> bool:
    return any(p.search(text) for p in LIMIT_PATTERNS)


def classify_result_event(event: dict[str, Any]) -> StopReason:
    """
    Classify a 'result' event from claude --output-format stream-json.

    Expected shapes:
      {"type": "result", "subtype": "success", ...}
      {"type": "result", "subtype": "error_during_tool_use", "error": {...}}
    """
    subtype = event.get("subtype", "")
    error_info = event.get("error") or {}
    error_msg = ""

    if isinstance(error_info, dict):
        error_msg = error_info.get("message", "") or ""
    elif isinstance(error_info, str):
        error_msg = error_info

    # Also check top-level message field
    top_msg = event.get("message", "") or ""

    if subtype in COMPLETION_SUBTYPES:
        return StopReason.COMPLETED

    if is_limit_message(error_msg) or is_limit_message(top_msg):
        return StopReason.LIMIT_HIT

    if subtype in ERROR_SUBTYPES:
        return StopReason.FAILED

    return StopReason.UNKNOWN


def classify_line(raw_line: str) -> StopReason | None:
    """
    Quick check on a raw text line (fallback for non-JSON output).
    Returns None if the line is not a stop signal.
    """
    if is_limit_message(raw_line):
        return StopReason.LIMIT_HIT
    return None


def extract_session_id(event: dict[str, Any]) -> str | None:
    """Pull the session_id from a system/init event if present."""
    return event.get("session_id") or event.get("sessionId")

"""Tests for stop-reason detection logic."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
from orchestrator.detector import (
    StopReason,
    classify_result_event,
    classify_line,
    is_limit_message,
    parse_reset_time,
    parse_reset_time_from_event,
)


class TestIsLimitMessage:
    def test_usage_limit(self):
        assert is_limit_message("You've reached your usage limit for this period.")

    def test_rate_limit(self):
        assert is_limit_message("rate limit exceeded, please wait")

    def test_rate_limit_hyphen(self):
        assert is_limit_message("rate-limit hit")

    def test_too_many_requests_full(self):
        assert is_limit_message("Too many requests, please try again.")

    def test_too_many_requests(self):
        assert is_limit_message("Too many requests.")

    def test_five_hour_limit(self):
        assert is_limit_message("5 hour limit reached")

    def test_weekly_limit(self):
        assert is_limit_message("Your weekly limit has been reached.")

    def test_plan_limit(self):
        assert is_limit_message("plan limit exceeded")

    def test_session_limit(self):
        assert is_limit_message("You've hit your session limit")

    def test_full_claude_limit_message(self):
        # The session limit phrase matches without needing "resets" pattern
        assert is_limit_message("You've hit your session limit · resets 1:20pm (Asia/Kolkata)")

    def test_529_status(self):
        assert is_limit_message("HTTP 529 overloaded")

    def test_529_status(self):
        assert is_limit_message("HTTP 529 error")

    def test_normal_message(self):
        assert not is_limit_message("Task completed successfully.")

    def test_empty(self):
        assert not is_limit_message("")

    def test_case_insensitive(self):
        assert is_limit_message("USAGE LIMIT REACHED")


class TestClassifyResultEvent:
    def test_success(self):
        event = {"type": "result", "subtype": "success"}
        assert classify_result_event(event) == StopReason.COMPLETED

    def test_429_api_error_status_is_limit(self):
        # Primary signal: api_error_status 429
        event = {
            "type": "result",
            "subtype": "success",
            "api_error_status": 429,
            "result": "You've hit your session limit · resets 1:20pm (Asia/Kolkata)",
        }
        assert classify_result_event(event) == StopReason.LIMIT_HIT

    def test_529_api_error_status_is_limit(self):
        event = {"type": "result", "subtype": "success", "api_error_status": 529}
        assert classify_result_event(event) == StopReason.LIMIT_HIT

    def test_success_with_no_api_error_is_completed(self):
        # Normal completion — result text may contain "limit" in other contexts
        event = {
            "type": "result",
            "subtype": "success",
            "api_error_status": None,
            "result": "Done. The rate limit on the API is 100 req/min.",
        }
        assert classify_result_event(event) == StopReason.COMPLETED

    def test_error_during_tool_use_with_limit_message(self):
        event = {
            "type": "result",
            "subtype": "error_during_tool_use",
            "error": {"message": "usage limit reached"},
        }
        assert classify_result_event(event) == StopReason.LIMIT_HIT

    def test_error_during_tool_use_non_limit(self):
        event = {
            "type": "result",
            "subtype": "error_during_tool_use",
            "error": {"message": "bash command failed with exit code 1"},
        }
        assert classify_result_event(event) == StopReason.FAILED

    def test_limit_in_top_level_message(self):
        event = {
            "type": "result",
            "subtype": "error",
            "message": "rate limit exceeded",
        }
        assert classify_result_event(event) == StopReason.LIMIT_HIT

    def test_unknown_subtype(self):
        event = {"type": "result", "subtype": "something_else"}
        assert classify_result_event(event) == StopReason.UNKNOWN

    def test_string_error_field_rate_limit(self):
        event = {
            "type": "result",
            "subtype": "error",
            "error": "rate limit exceeded",
        }
        assert classify_result_event(event) == StopReason.LIMIT_HIT


class TestClassifyLine:
    def test_limit_line(self):
        assert classify_line("Error: usage limit reached") == StopReason.LIMIT_HIT

    def test_normal_line(self):
        assert classify_line("Writing file src/main.py") is None

    def test_empty_line(self):
        assert classify_line("") is None


class TestParseResetTimeFromEvent:
    def test_extracts_unix_timestamp(self):
        # resetsAt=1779729000 → 2026-05-25 17:10:00 UTC
        event = {"type": "rate_limit_event", "rate_limit_info": {"resetsAt": 1779729000}}
        result = parse_reset_time_from_event(event)
        assert result == datetime(2026, 5, 25, 17, 10, 0)

    def test_returns_none_when_no_rate_limit_info(self):
        assert parse_reset_time_from_event({"type": "result"}) is None

    def test_returns_none_when_no_resets_at(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}
        assert parse_reset_time_from_event(event) is None

    def test_past_timestamp_returned_as_is(self):
        # Caller checks if past — we just parse
        event = {"type": "rate_limit_event", "rate_limit_info": {"resetsAt": 1000000000}}
        result = parse_reset_time_from_event(event)
        assert result == datetime(2001, 9, 9, 1, 46, 40)


class TestParseResetTime:
    def test_future_reset_returns_utc(self):
        # 12:00 UTC = 17:30 IST. Reset at 10:40pm IST = 17:10 UTC — within 2h, return it
        now = datetime(2026, 5, 25, 12, 0, 0)
        result = parse_reset_time("resets 10:40pm (Asia/Kolkata)", _now_utc=now)
        assert result == datetime(2026, 5, 25, 17, 10, 0)

    def test_past_reset_returns_immediate(self):
        # 18:00 UTC = 23:30 IST. Reset at 10:40pm IST (17:10 UTC) — already past
        now = datetime(2026, 5, 25, 18, 0, 0)
        result = parse_reset_time("resets 10:40pm (Asia/Kolkata)", _now_utc=now)
        assert result == now  # returns now_utc immediately

    def test_stale_message_over_6h_returns_immediate(self):
        # 06:00 UTC = 11:30 IST. Reset at 10:40pm IST = ~11h away → stale, resume now
        now = datetime(2026, 5, 25, 6, 0, 0)
        result = parse_reset_time("resets 10:40pm (Asia/Kolkata)", _now_utc=now)
        assert result == now

    def test_no_match_returns_none(self):
        assert parse_reset_time("Task completed successfully") is None

    def test_12pm_noon(self):
        # 05:00 UTC = 10:30 IST. Reset at 12:00pm IST = 06:30 UTC — within 2h
        now = datetime(2026, 5, 25, 5, 0, 0)
        result = parse_reset_time("resets 12:00pm (Asia/Kolkata)", _now_utc=now)
        assert result == datetime(2026, 5, 25, 6, 30, 0)  # 12:00pm IST = 06:30 UTC

    def test_12am_midnight_already_past(self):
        # 17:00 UTC = 22:30 IST. "12:00am IST" = midnight today IST = 00:00 IST
        # which is already past (00:00 < 22:30) → resume immediately
        now = datetime(2026, 5, 25, 17, 0, 0)
        result = parse_reset_time("resets 12:00am (Asia/Kolkata)", _now_utc=now)
        assert result == now

    def test_12am_midnight_future(self):
        # 12:00 UTC = 17:30 IST. "12:00am IST" = midnight = 00:00 IST → past → immediate
        # Use a time where midnight IST is still in the future: 10:00 UTC = 15:30 IST
        now = datetime(2026, 5, 25, 10, 0, 0)
        result = parse_reset_time("resets 12:00am (Asia/Kolkata)", _now_utc=now)
        # 12:00am IST = 18:30 UTC, which is 8.5h away → >6h guard → immediate
        assert result == now

    def test_within_1h_future(self):
        # 16:20 UTC = 21:50 IST. Reset at 10:40pm IST = 17:10 UTC — 50min away
        now = datetime(2026, 5, 25, 16, 20, 0)
        result = parse_reset_time("resets 10:40pm (Asia/Kolkata)", _now_utc=now)
        assert result == datetime(2026, 5, 25, 17, 10, 0)

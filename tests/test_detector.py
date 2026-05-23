"""Tests for stop-reason detection logic."""

import pytest
from orchestrator.detector import (
    StopReason,
    classify_result_event,
    classify_line,
    is_limit_message,
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

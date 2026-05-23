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

    def test_try_again_later(self):
        assert is_limit_message("Please try again later.")

    def test_too_many_requests(self):
        assert is_limit_message("Too many requests.")

    def test_five_hour_limit(self):
        assert is_limit_message("5 hour limit reached")

    def test_weekly_limit(self):
        assert is_limit_message("Your weekly limit has been reached.")

    def test_plan_limit(self):
        assert is_limit_message("plan limit exceeded")

    def test_overloaded(self):
        assert is_limit_message("Claude is currently overloaded.")

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

    def test_string_error_field(self):
        event = {
            "type": "result",
            "subtype": "error",
            "error": "try again later",
        }
        assert classify_result_event(event) == StopReason.LIMIT_HIT


class TestClassifyLine:
    def test_limit_line(self):
        assert classify_line("Error: usage limit reached") == StopReason.LIMIT_HIT

    def test_normal_line(self):
        assert classify_line("Writing file src/main.py") is None

    def test_empty_line(self):
        assert classify_line("") is None

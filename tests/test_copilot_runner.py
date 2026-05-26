"""Unit tests for Copilot runner helper behavior."""

from orchestrator.copilot_runner import (
    _is_retryable_status,
    _parse_tool_arguments,
    _retry_delay_seconds,
    _trim_messages_for_budget,
)


class TestParseToolArguments:
    def test_parses_plain_json_object(self):
        args, err = _parse_tool_arguments('{"path":"README.md"}')
        assert err is None
        assert args == {"path": "README.md"}

    def test_parses_fenced_json(self):
        raw = """```json
{"command":"echo hi"}
```"""
        args, err = _parse_tool_arguments(raw)
        assert err is None
        assert args == {"command": "echo hi"}

    def test_allows_trailing_text_via_raw_decode(self):
        args, err = _parse_tool_arguments('{"path":"x"} trailing')
        assert args == {"path": "x"}
        assert err is not None
        assert "ignored trailing characters" in err

    def test_rejects_non_object_json(self):
        args, err = _parse_tool_arguments('[1,2,3]')
        assert args == {}
        assert err is not None
        assert "must decode to object" in err

    def test_invalid_json_returns_error(self):
        args, err = _parse_tool_arguments('{"path":')
        assert args == {}
        assert err is not None
        assert "invalid JSON args" in err


class TestRetryPolicy:
    def test_retryable_statuses(self):
        assert _is_retryable_status(503) is True
        assert _is_retryable_status(500) is True

    def test_non_retryable_status(self):
        assert _is_retryable_status(404) is False

    def test_retry_backoff(self):
        assert _retry_delay_seconds(0) == 1.0
        assert _retry_delay_seconds(1) == 2.0
        assert _retry_delay_seconds(2) == 4.0


class TestMessageBudget:
    def test_trims_old_middle_messages(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "a" * 50},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "b" * 50},
            {"role": "user", "content": "latest"},
        ]

        trimmed = _trim_messages_for_budget(
            messages,
            max_messages=4,
            max_chars=120,
            keep_head=1,
        )

        assert trimmed[0]["role"] == "system"
        assert trimmed[-1]["content"] == "latest"
        assert len(trimmed) <= 4

    def test_no_trim_when_under_budget(self):
        messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]
        trimmed = _trim_messages_for_budget(
            messages,
            max_messages=10,
            max_chars=1000,
            keep_head=0,
        )
        assert trimmed == messages

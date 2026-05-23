import io
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from orchestrator.job import Job
from orchestrator.runner import run_job
from orchestrator.store import Store


@pytest.fixture
def tmp_store(tmp_path):
    return Store(db_path=tmp_path / "test.db")


def _make_result_line(input_tokens=100, output_tokens=50):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


@pytest.mark.asyncio
async def test_run_job_accumulates_tokens(tmp_store):
    job = Job(prompt="hello")
    tmp_store.save_job(job)
    assert job.input_tokens == 0
    assert job.output_tokens == 0

    result_line = _make_result_line(input_tokens=200, output_tokens=80)
    lines = [
        b'{"type":"system","session_id":"sess1"}\n',
        result_line.encode() + b"\n",
    ]

    async def fake_readline():
        if lines:
            return lines.pop(0)
        return b""

    mock_proc = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = fake_readline
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock()

    broadcast = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await run_job(job, tmp_store, broadcast)

    assert job.input_tokens == 200
    assert job.output_tokens == 80


@pytest.mark.asyncio
async def test_run_job_accumulates_tokens_across_resumes(tmp_store):
    job = Job(prompt="hello")
    job.session_id = "sess1"
    job.input_tokens = 100
    job.output_tokens = 40
    tmp_store.save_job(job)

    result_line = _make_result_line(input_tokens=150, output_tokens=60)
    lines = [result_line.encode() + b"\n"]

    async def fake_readline():
        if lines:
            return lines.pop(0)
        return b""

    mock_proc = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = fake_readline
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock()

    broadcast = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await run_job(job, tmp_store, broadcast)

    assert job.input_tokens == 250   # 100 + 150
    assert job.output_tokens == 100  # 40 + 60

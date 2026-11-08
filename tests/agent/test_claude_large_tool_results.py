"""A large tool result survives the agent session.

The real bundled Claude CLI reads an attached image for a fake Messages API.
The CLI writes the image into one stream-json line twice, so a photo of a few
hundred kilobytes makes a line over the SDK's 1 MiB default, which killed the
session and the job.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from openscientist.agent import claude_code_agent
from openscientist.agent.base import AgentConfig, TurnOutcome
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from tests.agent.fake_anthropic_api import ReadFile, Say, answered_tool, serving
from tests.helpers import StubClaudeProvider

_SDK_DEFAULT_BUFFER = 1024 * 1024


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    """A job whose data holds a noisy photo, which compresses poorly."""
    job = tmp_path / "job"
    (job / "data").mkdir(parents=True)
    pixels = np.random.default_rng(7).integers(0, 256, (800, 1100, 3), dtype=np.uint8)
    Image.fromarray(pixels).save(job / "data" / "gel.jpg", "JPEG", quality=94)
    return job


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI keeps its state in a throwaway home and talks to nothing else."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "1")
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
        monkeypatch.delenv(name, raising=False)


def _agent(job: Path) -> ClaudeCodeAgent:
    config = AgentConfig(
        job_dir=job, data_file=None, system_prompt="test", model_override="claude-fake"
    )
    return ClaudeCodeAgent(config, StubClaudeProvider())


def _last_user_text(body: dict[str, object]) -> str:
    """The text of the request's last user message. The CLI appends system
    messages after it."""
    messages = body.get("messages") or []
    assert isinstance(messages, list)
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    content = users[-1].get("content") if users else ""
    if isinstance(content, str):
        return content
    assert isinstance(content, list)
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


def _read_then_answer(body: dict[str, object]) -> ReadFile | Say:
    return Say("Seen.") if answered_tool(body) else ReadFile("data/gel.jpg")


def test_the_session_carries_any_tool_result_the_api_could_take(tmp_path: Path) -> None:
    """The bound covers a result as large as a Messages API request, in both
    copies the CLI writes."""
    options = _agent(tmp_path)._build_options()
    assert options.max_buffer_size == claude_code_agent.MAX_MESSAGE_BYTES
    assert claude_code_agent.MAX_MESSAGE_BYTES > 2 * 32 * 1024 * 1024


@pytest.mark.asyncio
async def test_the_photo_overflows_the_sdk_default(
    job_dir: Path, cli_home: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precondition of the next test: at the SDK's default bound, the CLI's
    message for this photo kills the turn, in both of its sessions."""
    monkeypatch.setattr(claude_code_agent, "MAX_MESSAGE_BYTES", _SDK_DEFAULT_BUFFER)
    with serving(_read_then_answer) as (base_url, _api):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
        agent = _agent(job_dir)
        try:
            result = await agent.run_iteration("Look at the gel.", reset_session=True)
        finally:
            await agent.shutdown()
    assert result.outcome is TurnOutcome.FAILED


@pytest.mark.asyncio
async def test_an_attached_photo_is_read_without_killing_the_turn(
    job_dir: Path, cli_home: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(_read_then_answer) as (base_url, api):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
        agent = _agent(job_dir)
        try:
            result = await agent.run_iteration("Look at the gel.", reset_session=True)
        finally:
            await agent.shutdown()
    assert result.outcome is TurnOutcome.COMPLETED, result.error
    assert result.output == "Seen."
    assert result.tool_calls == 1
    assert answered_tool(api.requests[-1])


@pytest.mark.asyncio
async def test_an_oversized_result_retries_the_turn_in_a_fresh_session(
    job_dir: Path, cli_home: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the bound, the turn starts over in a fresh session that is told why,
    so the model can work around the file instead of the job failing."""
    monkeypatch.setattr(claude_code_agent, "MAX_MESSAGE_BYTES", 512 * 1024)

    def reply(body: dict[str, object]) -> ReadFile | Say:
        if claude_code_agent.OVERSIZED_RESULT_NOTE in _last_user_text(body):
            return Say("Worked from the data instead.")
        return _read_then_answer(body)

    with serving(reply) as (base_url, api):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
        agent = _agent(job_dir)
        try:
            result = await agent.run_iteration("Look at the gel.", reset_session=True)
        finally:
            await agent.shutdown()
    assert result.outcome is TurnOutcome.COMPLETED, result.error
    assert result.output == "Worked from the data instead."
    retried = api.requests[-1]
    assert claude_code_agent.OVERSIZED_RESULT_NOTE in _last_user_text(retried)
    assert "Look at the gel." in _last_user_text(retried)
    assert not answered_tool(retried), "the retry did not start a fresh session"


@pytest.mark.asyncio
async def test_a_turn_that_overflows_again_fails_with_a_plain_reason(
    job_dir: Path, cli_home: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_code_agent, "MAX_MESSAGE_BYTES", 512 * 1024)
    with serving(_read_then_answer) as (base_url, api):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
        agent = _agent(job_dir)
        try:
            result = await agent.run_iteration("Look at the gel.", reset_session=True)
        finally:
            await agent.shutdown()
    assert result.outcome is TurnOutcome.FAILED
    assert result.error.startswith("A tool result was too large")
    assert "JSON" not in result.error
    assert sum(1 for body in api.requests if not answered_tool(body)) == 2

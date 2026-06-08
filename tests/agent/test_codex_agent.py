"""Tests for `CodexAgent` — config/AGENTS.md wiring and the run loop.

The official ``openai-codex`` SDK is mocked (we patch
``codex_agent.AsyncCodex``), so no codex binary or network is required; the
tests exercise the agent's own logic: the written ``config.toml``/``AGENTS.md``,
thread lifecycle, usage mapping, and failure handling.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai_codex import ApprovalMode, Sandbox

from openscientist.agent.base import AbstractAgent, AgentConfig, TokenUsage
from openscientist.agent.codex_agent import CodexAgent
from tests.helpers import StubCodexProvider


class _Provider(StubCodexProvider):
    """Codex stub with a realistic provider config + auth env."""

    def codex_config_overrides(self) -> list[str]:
        return ["[model_providers.openai]", 'base_url = "https://api.openai.com/v1"']

    def codex_model_provider_id(self) -> str:
        return "openai"

    def codex_model_name(self) -> str:
        return "gpt-test"

    def codex_sdk_env(self) -> dict[str, str]:
        return {"OPENAI_API_KEY": "sk-secret"}


class _Item:
    """Minimal stand-in for an SDK thread item; only ``model_dump`` is used."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return dict(self._fields)


def _usage(*, input_tokens: int, cached: int, output: int, reasoning: int = 0) -> SimpleNamespace:
    """Build a usage payload shaped like the SDK's ThreadTokenUsage (``.last``
    is the per-turn TokenUsageBreakdown)."""
    last = SimpleNamespace(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output,
        reasoning_output_tokens=reasoning,
    )
    return SimpleNamespace(last=last, total=last)


def _agent(tmp_path: Path, **cfg_kwargs: object) -> CodexAgent:
    config = AgentConfig(job_dir=tmp_path, **cfg_kwargs)  # type: ignore[arg-type]
    return CodexAgent(config, _Provider())


def _turn(
    *, final: str = "done", items: list[Any] | None = None, usage: Any = None
) -> SimpleNamespace:
    if items is None:
        items = [
            _Item(id="a1", type="agentMessage", text=final),
            _Item(
                id="c1",
                type="commandExecution",
                command="ls",
                aggregated_output="out",
                exit_code=0,
                status="completed",
            ),
        ]
    return SimpleNamespace(items=items, final_response=final, usage=usage)


def _patch_codex(turn: SimpleNamespace) -> tuple[MagicMock, MagicMock]:
    """Return (MockAsyncCodex, thread) with ``thread.run`` returning ``turn``."""
    mock_codex_cls = MagicMock(name="AsyncCodex")
    inst = mock_codex_cls.return_value
    thread = MagicMock(name="Thread")
    thread.run = AsyncMock(return_value=turn)
    inst.thread_start = AsyncMock(return_value=thread)
    inst.close = AsyncMock()
    return mock_codex_cls, thread


# ── scaffold (unchanged contract) ──────────────────────────────────────


def test_codex_agent_is_abstract_agent_subclass() -> None:
    assert issubclass(CodexAgent, AbstractAgent)


def test_construct_exposes_config_and_provider(tmp_path: Path) -> None:
    config = AgentConfig(job_dir=tmp_path)
    provider = _Provider()
    agent = CodexAgent(config, provider)
    assert agent.config is config
    assert agent.provider is provider
    assert agent.total_tokens == TokenUsage()


# ── run loop ───────────────────────────────────────────────────────────


async def test_run_iteration_success(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    turn = _turn(final="the answer", usage=_usage(input_tokens=100, cached=20, output=50))
    mock_codex_cls, thread = _patch_codex(turn)

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("hello")

    assert result.success is True
    assert result.output == "the answer"
    # one non-message item -> one tool call
    assert result.tool_calls == 1
    assert [e.type for e in result.transcript] == ["assistant_text", "shell_execution"]
    thread.run.assert_awaited_once_with("hello")


async def test_run_iteration_cuts_runaway_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that exceeds the wall-clock bound is cut so a looping model cannot
    hang the job. success stays True (the discovery loop raises on failure) and
    the runaway client is torn down."""
    import asyncio

    from openscientist.agent import codex_agent

    monkeypatch.setattr(codex_agent, "_TURN_TIMEOUT_SECONDS", 0.01)
    agent = _agent(tmp_path)
    mock_codex_cls = MagicMock(name="AsyncCodex")
    inst = mock_codex_cls.return_value
    thread = MagicMock(name="Thread")

    async def never_ends(_prompt: str) -> object:
        await asyncio.sleep(5)  # exceeds the 0.01s bound
        return _turn()

    thread.run = never_ends
    inst.thread_start = AsyncMock(return_value=thread)
    inst.close = AsyncMock()

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("go")

    assert result.success is True  # job keeps going
    assert result.tool_calls == 0
    assert result.transcript == []
    inst.close.assert_awaited()  # runaway turn torn down


async def test_usage_subtraction_accumulates(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    turn = _turn(usage=_usage(input_tokens=100, cached=20, output=50))
    mock_codex_cls, _ = _patch_codex(turn)

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("hi")

    tu = agent.total_tokens
    assert tu.input_tokens == 80  # 100 - 20 cached
    assert tu.cache_read_tokens == 20
    assert tu.output_tokens == 50
    assert tu.cache_write_tokens == 0
    assert tu.reasoning_tokens == 0


def test_usage_from_payload_math() -> None:
    tu = CodexAgent._usage_from_payload(_usage(input_tokens=30, cached=12, output=7, reasoning=3))
    assert tu == TokenUsage(
        input_tokens=18,
        output_tokens=7,
        cache_read_tokens=12,
        cache_write_tokens=0,
        reasoning_tokens=3,
    )


def test_usage_from_payload_none_last() -> None:
    assert CodexAgent._usage_from_payload(SimpleNamespace(last=None)) == TokenUsage()


async def test_run_iteration_writes_config_and_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex passes only the config env table to the MCP child, never its own
    # process env, so the parent environment must be forwarded explicitly.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    agent = _agent(tmp_path, system_prompt="do science", use_hypotheses=True)
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("go")

    cfg = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert cfg["model_provider"] == "openai"
    assert cfg["model_providers"]["openai"]["base_url"] == "https://api.openai.com/v1"
    mcp = cfg["mcp_servers"]["openscientist-tools"]
    assert mcp["args"] == ["-m", "openscientist_tools"]
    assert mcp["env"]["OPENSCIENTIST_JOB_DIR"] == str(tmp_path)
    assert mcp["env"]["OPENSCIENTIST_USE_HYPOTHESES"] == "1"
    assert mcp["env"]["DATABASE_URL"] == "postgresql+asyncpg://u:p@db:5432/x"
    assert (tmp_path / "AGENTS.md").read_text() == "do science"

    # CodexConfig points the child at the per-job config home + provider auth.
    config_arg = mock_codex_cls.call_args.args[0]
    assert config_arg.env["CODEX_HOME"] == str(tmp_path / ".codex")
    assert config_arg.env["OPENAI_API_KEY"] == "sk-secret"
    assert config_arg.cwd == str(tmp_path)


async def test_relative_job_dir_resolved_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-page chat passes a relative job_dir (e.g. ``jobs/<id>``). It must
    be resolved to an absolute path: codex resolves a relative CODEX_HOME/cwd
    against its own working dir, doubling the path and failing with
    "CODEX_HOME ... does not exist"."""
    monkeypatch.chdir(tmp_path)
    rel = Path("jobs/job-abc")
    (tmp_path / rel).mkdir(parents=True)
    expected = (tmp_path / rel).resolve()

    agent = CodexAgent(AgentConfig(job_dir=rel, system_prompt="sp"), _Provider())
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("go")

    config_arg = mock_codex_cls.call_args.args[0]
    assert Path(config_arg.env["CODEX_HOME"]).is_absolute()
    assert config_arg.env["CODEX_HOME"] == str(expected / ".codex")
    assert config_arg.cwd == str(expected)
    assert mock_codex_cls.return_value.thread_start.call_args.kwargs["cwd"] == str(expected)

    cfg = tomllib.loads((expected / ".codex" / "config.toml").read_text())
    assert cfg["mcp_servers"]["openscientist-tools"]["env"]["OPENSCIENTIST_JOB_DIR"] == str(
        expected
    )
    assert (expected / "AGENTS.md").read_text() == "sp"


async def test_no_agents_md_without_system_prompt(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("go")
    assert not (tmp_path / "AGENTS.md").exists()


async def test_thread_is_reused_then_reset(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    inst = mock_codex_cls.return_value

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("one")
        await agent.run_iteration("two")  # reuses cached thread + client
        assert inst.thread_start.await_count == 1
        assert mock_codex_cls.call_count == 1
        await agent.run_iteration("three", reset_session=True)  # new thread, same client
        assert inst.thread_start.await_count == 2
        assert mock_codex_cls.call_count == 1


async def test_run_failure_returns_error_and_rebuilds(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    mock_codex_cls = MagicMock(name="AsyncCodex")
    inst = mock_codex_cls.return_value
    thread = MagicMock(name="Thread")
    thread.run = AsyncMock(side_effect=RuntimeError("boom"))
    inst.thread_start = AsyncMock(return_value=thread)
    inst.close = AsyncMock()

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("go")
        assert result.success is False
        assert "boom" in result.error
        assert result.output == ""
        inst.close.assert_awaited()  # client torn down on failure
        # client cleared -> next call rebuilds it
        thread.run = AsyncMock(return_value=_turn(usage=None))
        await agent.run_iteration("retry")
        assert mock_codex_cls.call_count == 2


async def test_shutdown_closes_client(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("go")
    assert agent._thread is not None
    await agent.shutdown()
    assert agent._thread is None
    assert agent._codex is None
    mock_codex_cls.return_value.close.assert_awaited()


# ── transcript bridge (SDK item -> model_dump -> CODEX) ────────────────


def test_to_transcript_empty() -> None:
    assert CodexAgent._to_transcript([]) == []


def test_to_transcript_routes_every_item_type() -> None:
    """``model_dump`` of each SDK item shape routes to the right variant."""
    items = [
        _Item(id="a", type="agentMessage", text="hi"),
        _Item(id="r", type="reasoning", summary=["think"], content=[]),
        _Item(
            id="c",
            type="commandExecution",
            command="ls",
            aggregated_output="o",
            exit_code=0,
            status="completed",
        ),
        _Item(
            id="f",
            type="fileChange",
            changes=[{"path": "x.py", "kind": {"type": "update"}}],
            status="completed",
        ),
        _Item(
            id="m",
            type="mcpToolCall",
            server="srv",
            tool="t",
            arguments={"q": 1},
            result={"content": [{"type": "text", "text": "res"}], "structured_content": None},
            error=None,
            status="completed",
        ),
        _Item(id="w", type="webSearch", query="cells"),
        _Item(id="p", type="plan", text="- step one"),
    ]
    entries = CodexAgent._to_transcript(items)
    assert [e.type for e in entries] == [
        "assistant_text",
        "reasoning",
        "shell_execution",
        "file_change",
        "tool_call",
        "tool_result",
        "web_search",
        "plan",
    ]
    assert not any(e.type == "unknown_entry" for e in entries)


def test_bridge_preserves_mcp_tool_call_result() -> None:
    """The nested MCP result survives ``model_dump``: the split yields a linked
    ToolCall + ToolResult with the flattened text."""
    item = _Item(
        id="call-7",
        type="mcpToolCall",
        server="openscientist-tools",
        tool="search",
        arguments={"query": "x"},
        result={
            "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
            "structured_content": {"hits": 2},
        },
        error=None,
        status="completed",
    )
    entries = CodexAgent._to_transcript([item])
    call = next(e for e in entries if e.type == "tool_call")
    res = next(e for e in entries if e.type == "tool_result")
    assert call.id == "call-7"
    assert call.tool == "search"
    assert res.call_id == "call-7"
    assert res.output == "first\nsecond"
    assert res.success is True
    assert res.structured_content == {"hits": 2}


def test_unknown_item_becomes_unknown_entry() -> None:
    item = _Item(id="u1", type="futureThing")
    entries = CodexAgent._to_transcript([item])
    assert len(entries) == 1
    assert entries[0].type == "unknown_entry"
    assert entries[0].source == "codex"


# ── thread options + auth provisioning ─────────────────────────────────


class _KeylessProvider(_Provider):
    """Codex stub with no API key (forces auth.json provisioning)."""

    def codex_sdk_env(self) -> dict[str, str]:
        return {}


async def test_thread_start_sandbox_full_access(tmp_path: Path) -> None:
    """The container is the sandbox, so codex gets full access."""
    agent = _agent(tmp_path)
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("go")
    kwargs = mock_codex_cls.return_value.thread_start.call_args.kwargs
    assert kwargs["sandbox"] == Sandbox.full_access


async def test_thread_start_approval_never(tmp_path: Path) -> None:
    """Headless runs have no human approver. deny_all maps to codex's "never"
    approval policy so tools run without an approval reviewer (auto_review's
    reviewer times out headless and fails every tool call)."""
    agent = _agent(tmp_path)
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("go")
    kwargs = mock_codex_cls.return_value.thread_start.call_args.kwargs
    assert kwargs["approval_mode"] == ApprovalMode.deny_all
    assert kwargs["model"] == "gpt-test"
    assert kwargs["model_provider"] == "openai"


def test_toml_str_escapes_control_chars() -> None:
    """Forwarded env values can contain quotes and newlines. The result must
    be a parseable TOML basic string."""
    from openscientist.agent.codex_agent import _toml_str

    value = 'a"b\\c\nd\te'
    parsed = tomllib.loads(f"k = {_toml_str(value)}")
    assert parsed["k"] == value


async def test_auth_not_provisioned_when_key_present(tmp_path: Path) -> None:
    agent = _agent(tmp_path)  # _Provider supplies OPENAI_API_KEY
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("go")
    assert not (tmp_path / ".codex" / "auth.json").exists()


async def test_auth_provisioned_from_codex_home_when_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text('{"tokens": {}}')

    config = AgentConfig(job_dir=tmp_path / "job")
    agent = CodexAgent(config, _KeylessProvider())
    mock_codex_cls, _ = _patch_codex(_turn(usage=None))
    with (
        patch("openscientist.agent.codex_agent.Path.home", return_value=fake_home),
        patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls),
    ):
        await agent.run_iteration("go")
    copied = config.job_dir / ".codex" / "auth.json"
    assert copied.exists()
    assert copied.read_text() == '{"tokens": {}}'

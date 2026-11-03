"""Tests for `CodexAgent` — config/AGENTS.md wiring and the run loop.

The official ``openai-codex`` SDK is mocked (we patch
``codex_agent.AsyncCodex``), so no codex binary or network is required. The
tests exercise the agent's own logic: the written ``config.toml``/``AGENTS.md``,
thread lifecycle, usage mapping, and failure handling.
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import (
    ItemCompletedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)
from openai_codex.models import Notification

from openscientist.agent.base import AbstractAgent, AgentConfig, TokenUsage, TurnOutcome
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
        return {"CODEX_API_KEY": "sk-secret"}


_THREAD_ID = "thread-1"
_TURN_ID = "turn-1"


def _usage(*, input_tokens: int, cached: int, output: int, reasoning: int = 0) -> Notification:
    """A usage report whose thread running ``total`` holds these counts, nested
    the way codex reports them: cached sits inside input, reasoning inside
    output. ``last`` holds a sliver, since it covers only the latest model
    request of the turn."""
    total = {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached,
        "outputTokens": output,
        "reasoningOutputTokens": reasoning,
        "totalTokens": input_tokens + output,
    }
    last = {
        "inputTokens": 1,
        "cachedInputTokens": 0,
        "outputTokens": 1,
        "reasoningOutputTokens": 0,
        "totalTokens": 2,
    }
    payload = ThreadTokenUsageUpdatedNotification.model_validate(
        {"threadId": _THREAD_ID, "turnId": _TURN_ID, "tokenUsage": {"total": total, "last": last}}
    )
    return Notification(method="thread/tokenUsage/updated", payload=payload)


def _item(item: dict[str, Any]) -> Notification:
    payload = ItemCompletedNotification.model_validate(
        {"completedAtMs": 0, "item": item, "threadId": _THREAD_ID, "turnId": _TURN_ID}
    )
    return Notification(method="item/completed", payload=payload)


def _completed(*, status: str = "completed", error: str | None = None) -> Notification:
    turn: dict[str, Any] = {"id": _TURN_ID, "items": [], "status": status}
    if error is not None:
        turn["error"] = {"message": error}
    payload = TurnCompletedNotification.model_validate({"threadId": _THREAD_ID, "turn": turn})
    return Notification(method="turn/completed", payload=payload)


def _agent(tmp_path: Path, **cfg_kwargs: object) -> CodexAgent:
    config = AgentConfig(job_dir=tmp_path, **cfg_kwargs)  # type: ignore[arg-type]
    return CodexAgent(config, _Provider())


def _turn(
    *,
    final: str = "done",
    items: list[dict[str, Any]] | None = None,
    usage: list[Notification] | None = None,
    end: Notification | None = None,
) -> list[Notification]:
    """The notifications of one turn: its items, its usage reports, its end."""
    if items is None:
        items = [
            {"id": "a1", "type": "agentMessage", "text": final},
            {
                "id": "c1",
                "type": "commandExecution",
                "command": "ls",
                "commandActions": [],
                "cwd": "/tmp",
                "aggregatedOutput": "out",
                "exitCode": 0,
                "source": "unifiedExecStartup",
                "status": "completed",
            },
        ]
    return [*map(_item, items), *(usage or []), end or _completed()]


def _handle(events: list[Notification], *, then_hang: bool = False) -> SimpleNamespace:
    """A started turn that streams ``events``, then hangs if asked."""

    async def stream() -> AsyncIterator[Notification]:
        for event in events:
            yield event
        if then_hang:
            await asyncio.sleep(5)

    return SimpleNamespace(id=_TURN_ID, stream=stream)


def _patch_codex(*turns: list[Notification]) -> tuple[MagicMock, MagicMock]:
    """Return (MockAsyncCodex, thread), whose turns stream ``turns`` in order,
    the last one repeating."""
    mock_codex_cls = MagicMock(name="AsyncCodex")
    inst = mock_codex_cls.return_value
    thread = MagicMock(name="Thread")
    queue = list(turns)

    def start(_prompt: str) -> SimpleNamespace:
        return _handle(queue.pop(0) if len(queue) > 1 else queue[0])

    thread.turn = AsyncMock(side_effect=start)
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


def test_mcp_env_merges_tool_server_env(tmp_path: Path) -> None:
    """AgentConfig.tool_server_env reaches the codex MCP server env table."""
    agent = _agent(tmp_path, tool_server_env={"OPENSCIENTIST_EXEC_TOKEN": "job-9.tok"})
    assert agent._mcp_env()["OPENSCIENTIST_EXEC_TOKEN"] == "job-9.tok"


# ── run loop ───────────────────────────────────────────────────────────


async def test_run_iteration_success(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    turn = _turn(final="the answer", usage=[_usage(input_tokens=100, cached=20, output=50)])
    mock_codex_cls, thread = _patch_codex(turn)

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("hello")

    assert result.success is True
    assert result.output == "the answer"
    # one non-message item -> one tool call
    assert result.tool_calls == 1
    assert [e.type for e in result.transcript] == ["assistant_text", "shell_execution"]
    thread.turn.assert_awaited_once_with("hello")


async def test_the_answer_is_the_final_answer_message(tmp_path: Path) -> None:
    """Commentary sent after the final answer does not replace it."""
    agent = _agent(tmp_path)
    items = [
        {"id": "a1", "type": "agentMessage", "text": "the answer", "phase": "final_answer"},
        {"id": "a2", "type": "agentMessage", "text": "an aside", "phase": "commentary"},
    ]
    mock_codex_cls, _ = _patch_codex(_turn(items=items))

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("hello")

    assert result.output == "the answer"


async def test_run_iteration_cuts_runaway_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that exceeds the wall-clock bound is cut so a looping model cannot
    hang the job, the runaway client is torn down, and the tokens the turn
    spent before the cut stay counted."""
    from openscientist.agent import codex_agent

    monkeypatch.setattr(codex_agent, "_TURN_TIMEOUT_SECONDS", 0.01)
    agent = _agent(tmp_path)
    mock_codex_cls, thread = _patch_codex([])
    spent = [_usage(input_tokens=100, cached=20, output=50)]
    thread.turn = AsyncMock(return_value=_handle(spent, then_hang=True))

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("go")

    # A wall-clock cut is reported honestly as TIMED_OUT (not a fake success);
    # the orchestrator decides whether to advance.
    assert result.outcome is TurnOutcome.TIMED_OUT
    assert result.success is False
    assert result.tool_calls == 0
    assert result.transcript == []
    mock_codex_cls.return_value.close.assert_awaited()  # runaway turn torn down
    assert agent.total_tokens == TokenUsage(input_tokens=80, output_tokens=50, cache_read_tokens=20)


async def test_a_turn_is_billed_for_every_model_request(tmp_path: Path) -> None:
    """Codex reports usage after each model request of a turn, and its ``last``
    covers only that request. The turn costs the growth of the thread's total,
    and a report repeated on a rate-limit update adds nothing."""
    agent = _agent(tmp_path)
    reports = [
        _usage(input_tokens=100, cached=20, output=50),
        _usage(input_tokens=250, cached=120, output=90, reasoning=10),
        _usage(input_tokens=250, cached=120, output=90, reasoning=10),
        _usage(input_tokens=400, cached=200, output=130, reasoning=10),
    ]
    mock_codex_cls, _ = _patch_codex(_turn(usage=reports))

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("hi")

    assert agent.total_tokens == TokenUsage(
        input_tokens=200, output_tokens=120, cache_read_tokens=200, reasoning_tokens=10
    )


async def test_each_turn_is_billed_what_it_added_to_its_thread(tmp_path: Path) -> None:
    """A thread's total runs across its turns, and a new thread starts at zero."""
    agent = _agent(tmp_path)
    mock_codex_cls, _ = _patch_codex(
        _turn(usage=[_usage(input_tokens=100, cached=0, output=10)]),
        _turn(usage=[_usage(input_tokens=300, cached=0, output=30)]),
        _turn(usage=[_usage(input_tokens=50, cached=0, output=5)]),
    )

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("one")
        await agent.run_iteration("two")
        assert agent.total_tokens == TokenUsage(input_tokens=300, output_tokens=30)
        await agent.run_iteration("three", reset_session=True)

    assert agent.total_tokens == TokenUsage(input_tokens=350, output_tokens=35)


async def test_a_filled_context_window_restarts_the_count(tmp_path: Path) -> None:
    """When the context window fills, codex sets its total to the window size
    with every bucket zeroed, then counts on from there."""
    agent = _agent(tmp_path)
    reports = [
        _usage(input_tokens=1000, cached=0, output=100),
        _usage(input_tokens=0, cached=0, output=0),
        _usage(input_tokens=200, cached=0, output=20),
    ]
    mock_codex_cls, _ = _patch_codex(_turn(usage=reports))

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        await agent.run_iteration("hi")

    assert agent.total_tokens == TokenUsage(input_tokens=1200, output_tokens=120)


async def test_a_failed_turn_reports_its_error(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    turn = _turn(
        usage=[_usage(input_tokens=100, cached=0, output=10)],
        end=_completed(status="failed", error="context too long"),
    )
    mock_codex_cls, _ = _patch_codex(turn)

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("hi")

    assert result.outcome is TurnOutcome.FAILED
    assert result.error == "context too long"
    assert agent.total_tokens == TokenUsage(input_tokens=100, output_tokens=10)


def test_usage_from_breakdown_math() -> None:
    """Both nested sub-counts leave their parent, so the buckets are disjoint
    and sum to the breakdown's own total of 37 rather than over-counting to 40."""
    counts = SimpleNamespace(
        input_tokens=30, cached_input_tokens=12, output_tokens=7, reasoning_output_tokens=3
    )
    assert CodexAgent._usage_from_breakdown(counts) == TokenUsage(
        input_tokens=18,
        output_tokens=4,
        cache_read_tokens=12,
        cache_write_tokens=0,
        reasoning_tokens=3,
    )


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
    assert config_arg.env["CODEX_API_KEY"] == "sk-secret"
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
    mock_codex_cls, thread = _patch_codex(_turn())
    inst = mock_codex_cls.return_value
    working_turn = thread.turn
    thread.turn = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("openscientist.agent.codex_agent.AsyncCodex", mock_codex_cls):
        result = await agent.run_iteration("go")
        assert result.success is False
        assert "boom" in result.error
        assert result.output == ""
        inst.close.assert_awaited()  # client torn down on failure
        # client cleared -> next call rebuilds it
        thread.turn = working_turn
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


class _Item:
    """Minimal stand-in for an SDK thread item. Only ``model_dump`` is used."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return dict(self._fields)


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


# ── thread options ─────────────────────────────────────────────────────


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

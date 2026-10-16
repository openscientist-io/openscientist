"""Unit tests for `ClaudeCodeAgent` and its standalone-MCP wiring."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import AgentDefinition, TextBlock, ToolUseBlock

from openscientist.agent.base import AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent, _IterationState
from openscientist.providers.base import ClaudeCompatible
from tests.helpers import StubClaudeProvider


class _StubProvider(StubClaudeProvider):
    """Stub provider with a configurable `claude_sdk_env`."""

    def __init__(self, *, sdk_env: dict[str, str] | None = None) -> None:
        self._sdk_env = sdk_env or {}

    def claude_sdk_env(self) -> dict[str, str]:
        return dict(self._sdk_env)


def _make_agent(
    tmp_path: Path,
    *,
    data_file: Path | None = None,
    data_files: list[Path] | None = None,
    use_hypotheses: bool = False,
    model_override: str | None = None,
    provider: ClaudeCompatible | None = None,
    tool_server_env: dict[str, str] | None = None,
    experts: Mapping[str, AgentDefinition] | None = None,
) -> ClaudeCodeAgent:
    config = AgentConfig(
        job_dir=tmp_path,
        data_file=data_file,
        system_prompt="test prompt",
        use_hypotheses=use_hypotheses,
        data_files=tuple(data_files or ()),
        model_override=model_override,
        tool_server_env=tool_server_env or {},
        experts=experts,
    )
    return ClaudeCodeAgent(config, provider or _StubProvider())


def test_build_options_uses_stdio_spec_for_openscientist_tools(tmp_path: Path) -> None:
    executor = _make_agent(tmp_path)
    options = executor._build_options()

    mcp_servers = options.mcp_servers
    assert isinstance(mcp_servers, dict)
    cfg = cast(dict[str, Any], mcp_servers["openscientist-tools"])
    assert cfg["type"] == "stdio"
    assert cfg["command"] == sys.executable
    assert cfg["args"] == ["-m", "openscientist_tools"]
    # The env dict carries the per-job overlays to the subprocess; missing it
    # would break the standalone MCP server at startup.
    assert "env" in cfg
    assert cfg["env"]["OPENSCIENTIST_JOB_ID"] == tmp_path.name
    assert cfg["env"]["OPENSCIENTIST_JOB_DIR"] == str(tmp_path)


def test_subprocess_env_passes_through_unrelated_openscientist_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`OPENSCIENTIST_*` env vars set by the parent (provider, model,
    executor settings, etc.) must reach the subprocess unmodified."""
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSCIENTIST_MODEL", "claude-sonnet-test")
    monkeypatch.setenv("OPENSCIENTIST_EXECUTOR_IMAGE", "custom-executor:latest")
    monkeypatch.setenv("OPENSCIENTIST_EXECUTOR_TIMEOUT", "180")

    env = _make_agent(tmp_path)._build_subprocess_env()
    assert env["OPENSCIENTIST_PROVIDER"] == "anthropic"
    assert env["OPENSCIENTIST_MODEL"] == "claude-sonnet-test"
    assert env["OPENSCIENTIST_EXECUTOR_IMAGE"] == "custom-executor:latest"
    assert env["OPENSCIENTIST_EXECUTOR_TIMEOUT"] == "180"


def test_subprocess_env_includes_job_id_and_job_dir(tmp_path: Path) -> None:
    executor = _make_agent(tmp_path)
    env = executor._build_subprocess_env()

    assert env["OPENSCIENTIST_JOB_ID"] == tmp_path.name
    assert env["OPENSCIENTIST_JOB_DIR"] == str(tmp_path)


def test_subprocess_env_merges_tool_server_env(tmp_path: Path) -> None:
    """AgentConfig.tool_server_env is merged into the subprocess env. The chat
    path injects the per-job exec token this way, never via global os.environ."""
    env = _make_agent(
        tmp_path,
        tool_server_env={"OPENSCIENTIST_EXEC_TOKEN": "job-9.tok", "X_EXTRA": "1"},
    )._build_subprocess_env()
    assert env["OPENSCIENTIST_EXEC_TOKEN"] == "job-9.tok"
    assert env["X_EXTRA"] == "1"


def test_subprocess_env_inherits_critical_parent_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test/db")
    monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", "test-key")
    monkeypatch.setenv("PATH", "/test/bin:/usr/bin")

    executor = _make_agent(tmp_path)
    env = executor._build_subprocess_env()

    assert env["DATABASE_URL"] == "postgresql+asyncpg://test/db"
    assert env["OPENSCIENTIST_SECRET_KEY"] == "test-key"
    assert env["PATH"] == "/test/bin:/usr/bin"


@pytest.mark.parametrize("use_hypotheses,expected", [(True, "1"), (False, "0")])
def test_subprocess_env_use_hypotheses_flag(
    tmp_path: Path, use_hypotheses: bool, expected: str
) -> None:
    executor = _make_agent(tmp_path, use_hypotheses=use_hypotheses)
    assert executor._build_subprocess_env()["OPENSCIENTIST_USE_HYPOTHESES"] == expected


def test_subprocess_env_data_file_optional(tmp_path: Path) -> None:
    data_file = tmp_path / "primary.csv"
    data_file.write_text("col\n1\n")

    with_file = _make_agent(tmp_path, data_file=data_file)._build_subprocess_env()
    assert with_file["OPENSCIENTIST_DATA_FILE"] == str(data_file)

    without_file = _make_agent(tmp_path, data_file=None)._build_subprocess_env()
    assert "OPENSCIENTIST_DATA_FILE" not in without_file


def test_subprocess_env_data_files_pathsep_joined(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    executor = _make_agent(tmp_path, data_files=[a, b])
    env = executor._build_subprocess_env()
    assert env["OPENSCIENTIST_DATA_FILES"] == f"{a}{os.pathsep}{b}"


def test_subprocess_env_data_files_empty_unsets_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_DATA_FILES", "/should/be/removed")
    executor = _make_agent(tmp_path, data_files=None)
    env = executor._build_subprocess_env()
    assert "OPENSCIENTIST_DATA_FILES" not in env


def test_build_options_uses_model_override_when_set(tmp_path: Path) -> None:
    agent = _make_agent(tmp_path, model_override="custom-model")
    options = agent._build_options()

    assert options.cwd == str(tmp_path)
    assert options.model == "custom-model"


def test_build_options_defaults_to_provider_model(tmp_path: Path) -> None:
    """Without an override the model comes from the provider, not settings."""
    agent = _make_agent(tmp_path)
    options = agent._build_options()

    assert options.model == "stub-model"


def test_apply_provider_env_sets_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent pushes the provider's auth/routing env into the process so
    the SDK CLI and the tools subprocess inherit it."""
    # Seed via monkeypatch so teardown removes the var even though
    # `_apply_provider_env` mutates os.environ directly.
    monkeypatch.setenv("STUB_AUTH_TOKEN", "before")
    provider = _StubProvider(sdk_env={"STUB_AUTH_TOKEN": "secret"})
    agent = _make_agent(tmp_path, provider=provider)

    agent._apply_provider_env()

    assert os.environ["STUB_AUTH_TOKEN"] == "secret"


async def test_built_spec_spawns_subprocess_that_lists_all_tools(
    tmp_path: Path,
    test_database_url: str,
    _apply_migrations_once: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the spec the executor builds must actually spawn a working
    `openscientist_tools` subprocess whose tool list matches what the agent
    is supposed to see. Bypasses the SDK and connects directly via MCP
    stdio so we exercise the spec wiring without needing an LLM."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    monkeypatch.setenv("DATABASE_URL", test_database_url)

    executor = _make_agent(tmp_path, use_hypotheses=True)
    options = executor._build_options()
    mcp_servers = options.mcp_servers
    assert isinstance(mcp_servers, dict)
    cfg = cast(dict[str, Any], mcp_servers["openscientist-tools"])

    params = StdioServerParameters(
        command=cfg["command"],
        args=cfg["args"],
        env=cfg["env"],
        cwd=str(tmp_path),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}

    # The standalone server registers all 13 agent tools plus the
    # `ping` smoke tool. With `use_hypotheses=True` (set on the spec env),
    # add_hypothesis and update_hypothesis are included.
    assert "ping" in names
    assert {
        "read_document",
        "search_pubmed",
        "set_status",
        "set_job_title",
        "save_iteration_summary",
        "set_consensus_answer",
        "update_knowledge_state",
        "add_hypothesis",
        "update_hypothesis",
        "execute_code",
    } <= names


def test_build_options_without_experts_registers_no_agents(tmp_path: Path) -> None:
    """No experts configured leaves the SDK's `agents=` unset."""
    assert _make_agent(tmp_path)._build_options().agents is None


def test_build_options_keeps_empty_experts_distinct_from_absent(tmp_path: Path) -> None:
    """An empty mapping registers an empty agent set, not the absent default."""
    assert _make_agent(tmp_path, experts={})._build_options().agents == {}


def test_build_options_forwards_experts_to_sdk_agents(tmp_path: Path) -> None:
    """Every configured expert reaches `agents=` with its definition intact."""
    experts = {
        "foo": AgentDefinition(description="Foo expert", prompt="You are foo."),
        "bar": AgentDefinition(
            description="Bar expert",
            prompt="You are bar.",
            tools=["Read"],
            model="sonnet",
        ),
    }
    agents = _make_agent(tmp_path, experts=experts)._build_options().agents

    assert agents is not None
    assert set(agents) == {"foo", "bar"}
    assert agents["foo"].description == "Foo expert"
    assert agents["bar"].tools == ["Read"]
    assert agents["bar"].model == "sonnet"


def test_experts_do_not_disturb_the_other_options(tmp_path: Path) -> None:
    """Registering experts changes nothing else about the session."""
    experts = {"e": AgentDefinition(description="d", prompt="p")}
    with_experts = _make_agent(tmp_path, experts=experts)._build_options()
    without_experts = _make_agent(tmp_path)._build_options()

    assert with_experts.system_prompt == without_experts.system_prompt
    assert with_experts.cwd == without_experts.cwd
    assert with_experts.model == without_experts.model
    assert isinstance(with_experts.mcp_servers, dict)
    assert isinstance(without_experts.mcp_servers, dict)
    assert set(with_experts.mcp_servers) == set(without_experts.mcp_servers)


def test_experts_are_frozen_against_caller_mutation(tmp_path: Path) -> None:
    """Mutating the caller's mapping after construction cannot change the
    agents the session registers."""
    experts = {"one": AgentDefinition(description="d1", prompt="p1")}
    agent = _make_agent(tmp_path, experts=experts)

    experts["two"] = AgentDefinition(description="d2", prompt="p2")
    experts.pop("one")

    agents = agent._build_options().agents
    assert agents is not None
    assert set(agents) == {"one"}


class _StubClient:
    """Stands in for the SDK client, replaying a canned message stream."""

    def __init__(self, messages: Sequence[object]) -> None:
        self._messages = messages

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[object]:
        for message in self._messages:
            yield message


def _delegation(slug: str, *, name: str = "Agent") -> ToolUseBlock:
    """A tool-use block shaped like the SDK's subagent invocation."""
    return ToolUseBlock(id="t1", name=name, input={"subagent_type": slug})


def _handle(agent: ClaudeCodeAgent, *blocks: object) -> _IterationState:
    """Run one content list through the agent's stream handler."""
    state = _IterationState()
    agent._handle_content_list(list(blocks), state)
    return state


@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
def test_a_delegation_to_a_registered_expert_is_counted(tmp_path: Path, tool_name: str) -> None:
    """Both the current and the older SDK spelling of the subagent tool count."""
    agent = _make_agent(tmp_path, experts={"alpha": AgentDefinition(description="d", prompt="p")})
    state = _handle(agent, _delegation("alpha", name=tool_name))

    assert state.subagent_call_count == 1
    assert state.subagent_names == {"alpha"}
    assert state.subagent_log == ["alpha"]


def test_an_ordinary_tool_call_is_not_a_delegation(tmp_path: Path) -> None:
    """A normal tool still counts as a tool call and never as a delegation."""
    agent = _make_agent(tmp_path, experts={"alpha": AgentDefinition(description="d", prompt="p")})
    state = _handle(agent, ToolUseBlock(id="t1", name="execute_code", input={"code": "1"}))

    assert state.tool_call_count == 1
    assert state.subagent_call_count == 0
    assert state.subagent_log == []


def test_a_subagent_outside_the_roster_is_not_counted(tmp_path: Path) -> None:
    """An unregistered slug is not a delegation this run can attribute."""
    agent = _make_agent(tmp_path, experts={"alpha": AgentDefinition(description="d", prompt="p")})
    state = _handle(agent, _delegation("stranger"))

    assert state.subagent_call_count == 0
    assert state.subagent_names == set()


def test_no_registered_experts_means_no_delegations(tmp_path: Path) -> None:
    """With no roster there is nothing to delegate to, whatever the block says."""
    state = _handle(_make_agent(tmp_path), _delegation("alpha"))

    assert state.subagent_call_count == 0


def test_a_malformed_subagent_type_is_not_counted(tmp_path: Path) -> None:
    """A non-string or absent subagent_type must not be treated as a slug."""
    agent = _make_agent(tmp_path, experts={"alpha": AgentDefinition(description="d", prompt="p")})
    state = _handle(
        agent,
        ToolUseBlock(id="t1", name="Agent", input={"subagent_type": ["alpha"]}),
        ToolUseBlock(id="t2", name="Agent", input={}),
    )

    assert state.subagent_call_count == 0


def test_repeated_delegations_count_every_call_but_name_the_expert_once(tmp_path: Path) -> None:
    """The log counts invocations; the name set identifies who was used."""
    agent = _make_agent(
        tmp_path,
        experts={
            "alpha": AgentDefinition(description="d", prompt="p"),
            "beta": AgentDefinition(description="d", prompt="p"),
        },
    )
    state = _handle(
        agent,
        _delegation("alpha"),
        _delegation("beta"),
        _delegation("alpha"),
    )

    assert state.subagent_call_count == 3
    assert state.subagent_names == {"alpha", "beta"}
    assert state.subagent_log == ["alpha", "beta", "alpha"]


@pytest.mark.asyncio
async def test_delegation_counts_reach_the_iteration_result(tmp_path: Path) -> None:
    """The counters the orchestrator reads come off a completed turn."""
    agent = _make_agent(tmp_path, experts={"alpha": AgentDefinition(description="d", prompt="p")})
    turn = [SimpleNamespace(content=[_delegation("alpha"), TextBlock(text="done")])]

    with patch.object(agent, "_ensure_client", new=AsyncMock(return_value=_StubClient(turn))):
        result = await agent.run_iteration("go")

    assert result.success
    assert result.subagent_calls == 1
    assert result.subagent_names == frozenset({"alpha"})
    assert result.subagent_log == ("alpha",)


@pytest.mark.asyncio
async def test_a_turn_without_delegations_reports_none(tmp_path: Path) -> None:
    """A turn that never delegates leaves the counters empty, not unset."""
    agent = _make_agent(tmp_path, experts={"alpha": AgentDefinition(description="d", prompt="p")})
    turn = [SimpleNamespace(content=[TextBlock(text="done")])]

    with patch.object(agent, "_ensure_client", new=AsyncMock(return_value=_StubClient(turn))):
        result = await agent.run_iteration("go")

    assert result.subagent_calls == 0
    assert result.subagent_names == frozenset()
    assert result.subagent_log == ()

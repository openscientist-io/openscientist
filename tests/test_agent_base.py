"""Runtime tests for the generic `AbstractAgent` base and `AgentConfig`."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from openscientist.agent.base import (
    AbstractAgent,
    AgentBackend,
    AgentConfig,
    IterationResult,
    TokenUsage,
    TurnOutcome,
)
from openscientist.agent.mcp_specs import StdioMcpServerSpec
from openscientist.prompts.common import BackendFragments
from openscientist.providers.base import ClaudeCompatible
from tests.helpers import StubClaudeProvider as _StubProvider

_STUB_FRAGMENTS = BackendFragments(
    skills_location="the skills dir",
    builtin_read_tool="the read tool",
    builtin_read_tool_short="the read tool",
    search_skills_doc="",
    skills_discovery_note="",
)


class _StubAgent(AbstractAgent[ClaudeCompatible]):
    backend = AgentBackend.CLAUDE_CODE
    file_write_tool = "Write"

    async def run_iteration(self, prompt: str, *, reset_session: bool = False) -> IterationResult:
        return IterationResult(
            outcome=TurnOutcome.COMPLETED, output=prompt, tool_calls=0, transcript=[]
        )

    async def shutdown(self) -> None:
        return None

    @classmethod
    def prompt_fragments(cls) -> BackendFragments:
        return _STUB_FRAGMENTS

    @classmethod
    def discovery_system_prompt(
        cls, *, use_hypotheses: bool = False, phenix_available: bool = False
    ) -> str:
        return "stub discovery prompt"

    async def prepare_job_workspace(self, *, use_hypotheses: bool = False) -> None:
        return None


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(job_dir=tmp_path)


def test_abstract_agent_cannot_be_instantiated(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        AbstractAgent(_config(tmp_path), _StubProvider())  # type: ignore[abstract]


def test_incomplete_subclass_cannot_instantiate(tmp_path: Path) -> None:
    class _NoShutdown(AbstractAgent[ClaudeCompatible]):
        async def run_iteration(
            self, prompt: str, *, reset_session: bool = False
        ) -> IterationResult:
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
            )

        # shutdown intentionally omitted

    with pytest.raises(TypeError):
        _NoShutdown(_config(tmp_path), _StubProvider())  # type: ignore[abstract]


def test_complete_subclass_instantiates_and_exposes_config(tmp_path: Path) -> None:
    config = _config(tmp_path)
    provider = _StubProvider()
    agent = _StubAgent(config, provider)
    assert agent.config is config
    assert agent.provider is provider
    assert agent.total_tokens == TokenUsage()


async def test_run_iteration_returns_iteration_result(tmp_path: Path) -> None:
    agent = _StubAgent(_config(tmp_path), _StubProvider())
    result = await agent.run_iteration("hello")
    assert isinstance(result, IterationResult)
    assert result.output == "hello"


def test_total_tokens_returns_live_object(tmp_path: Path) -> None:
    """Concrete agents accumulate by mutating the same object the property
    returns; it must not be a defensive copy."""
    agent = _StubAgent(_config(tmp_path), _StubProvider())
    agent._token_usage.input_tokens += 7
    assert agent.total_tokens.input_tokens == 7
    assert agent.total_tokens is agent._token_usage


def test_agent_config_is_frozen(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.job_dir = tmp_path / "other"  # type: ignore[misc]


def test_agent_config_defaults(tmp_path: Path) -> None:
    config = AgentConfig(job_dir=tmp_path)
    assert config.data_file is None
    assert config.system_prompt is None
    assert config.use_hypotheses is False
    assert config.data_files == ()
    assert config.mcp_servers == ()


def test_agent_config_all_fields(tmp_path: Path) -> None:
    spec = StdioMcpServerSpec(name="tools", command="python")
    config = AgentConfig(
        job_dir=tmp_path,
        data_file=tmp_path / "primary.csv",
        system_prompt="do science",
        use_hypotheses=True,
        data_files=(tmp_path / "a.csv", tmp_path / "b.csv"),
        mcp_servers=(spec,),
    )
    assert config.data_file == tmp_path / "primary.csv"
    assert config.system_prompt == "do science"
    assert config.use_hypotheses is True
    assert config.data_files == (tmp_path / "a.csv", tmp_path / "b.csv")
    assert config.mcp_servers == (spec,)


def test_base_reexports() -> None:
    import openscientist.transcript as transcript_module
    from openscientist.agent import base

    assert base.IterationResult is IterationResult
    assert base.TokenUsage is TokenUsage
    assert base.TranscriptEntry is transcript_module.TranscriptEntry

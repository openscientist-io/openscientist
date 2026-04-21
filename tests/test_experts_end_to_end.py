"""End-to-end integration test for the expert subagent pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk.types import AgentDefinition
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.agent.sdk_executor import SDKAgentExecutor
from openscientist.job_chat import _build_chat_executor
from openscientist.orchestrator.discovery import _build_agent_executor

EXPECTED_SEEDED_SLUGS: frozenset[str] = frozenset(
    {
        "research-lead",
        "research-subagent",
        "citations-agent",
        "data-scientist",
        "python-pro",
        "scientific-literature-researcher",
        "data-researcher",
        "research-analyst",
    }
)


@pytest.mark.asyncio
async def test_discovery_executor_has_seeded_experts(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Discovery's executor factory picks up all 8 seeded experts."""
    _ = db_session
    executor = await _build_agent_executor(
        job_dir=tmp_path,
        data_file=None,
    )
    assert isinstance(executor, SDKAgentExecutor)
    options = executor._build_options()

    assert options.agents is not None
    assert EXPECTED_SEEDED_SLUGS.issubset(options.agents.keys()), (
        f"Missing seeded experts: {EXPECTED_SEEDED_SLUGS - set(options.agents.keys())}"
    )
    for slug in EXPECTED_SEEDED_SLUGS:
        agent_def = options.agents[slug]
        assert isinstance(agent_def, AgentDefinition)
        assert agent_def.description, f"{slug}: empty description"
        assert agent_def.prompt, f"{slug}: empty prompt"
        assert agent_def.model in {"sonnet", "opus", "haiku", "inherit"}


@pytest.mark.asyncio
async def test_chat_executor_has_seeded_experts(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Chat's executor factory picks up all 8 seeded experts."""
    _ = db_session
    executor = await _build_chat_executor(
        job_dir=tmp_path,
        system_prompt="test chat prompt",
    )
    assert isinstance(executor, SDKAgentExecutor)
    options = executor._build_options()

    assert options.agents is not None
    assert EXPECTED_SEEDED_SLUGS.issubset(options.agents.keys())


@pytest.mark.asyncio
async def test_discovery_and_chat_see_same_expert_set(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Discovery and chat see identical expert sets."""
    _ = db_session
    (tmp_path / "discovery").mkdir(exist_ok=True)
    (tmp_path / "chat").mkdir(exist_ok=True)

    discovery_executor = await _build_agent_executor(
        job_dir=tmp_path / "discovery",
        data_file=None,
    )
    chat_executor = await _build_chat_executor(
        job_dir=tmp_path / "chat",
        system_prompt="test",
    )
    assert isinstance(discovery_executor, SDKAgentExecutor)
    assert isinstance(chat_executor, SDKAgentExecutor)

    discovery_slugs = set(discovery_executor._build_options().agents or {})
    chat_slugs = set(chat_executor._build_options().agents or {})

    assert discovery_slugs == chat_slugs
    assert EXPECTED_SEEDED_SLUGS.issubset(discovery_slugs)

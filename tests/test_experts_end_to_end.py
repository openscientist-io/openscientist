"""End-to-end integration test for the expert subagent pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import AgentDefinition
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.job_chat import _load_chat_experts
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


async def _discovery_experts(job_dir: Path) -> dict[str, AgentDefinition]:
    """The roster discovery hands its agent, loaded from the real catalog."""
    with patch("openscientist.orchestrator.discovery.get_agent") as mock_get_agent:
        await _build_agent_executor(job_dir=job_dir, data_file=None)
    experts: dict[str, AgentDefinition] = mock_get_agent.call_args[0][0].experts
    return experts


@pytest.mark.asyncio
async def test_discovery_agent_receives_the_seeded_experts(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Discovery's agent-construction path picks up all 8 seeded experts."""
    _ = db_session
    experts = await _discovery_experts(tmp_path)

    assert EXPECTED_SEEDED_SLUGS.issubset(experts), (
        f"Missing seeded experts: {EXPECTED_SEEDED_SLUGS - set(experts)}"
    )
    for slug in EXPECTED_SEEDED_SLUGS:
        agent_def = experts[slug]
        assert isinstance(agent_def, AgentDefinition)
        assert agent_def.description, f"{slug}: empty description"
        assert agent_def.prompt, f"{slug}: empty prompt"
        assert agent_def.model in {"sonnet", "opus", "haiku", "inherit"}


@pytest.mark.asyncio
async def test_chat_turn_receives_the_seeded_experts(
    db_session: AsyncSession,
) -> None:
    """A chat turn's roster picks up all 8 seeded experts."""
    _ = db_session

    assert EXPECTED_SEEDED_SLUGS.issubset(await _load_chat_experts())


@pytest.mark.asyncio
async def test_discovery_and_chat_see_same_expert_set(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Discovery and chat read one catalog, so their rosters must agree."""
    _ = db_session
    (tmp_path / "discovery").mkdir(exist_ok=True)

    discovery_slugs = set(await _discovery_experts(tmp_path / "discovery"))
    chat_slugs = set(await _load_chat_experts())

    assert discovery_slugs == chat_slugs
    assert EXPECTED_SEEDED_SLUGS.issubset(discovery_slugs)

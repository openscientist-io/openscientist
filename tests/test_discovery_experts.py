"""Tests that the discovery agent-construction path loads experts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import AgentDefinition

from openscientist.orchestrator.discovery import _build_agent_executor


def _canned() -> dict[str, AgentDefinition]:
    return {
        "alpha": AgentDefinition(description="alpha agent", prompt="you are alpha"),
        "beta": AgentDefinition(description="beta agent", prompt="you are beta"),
    }


@pytest.mark.asyncio
async def test_discovery_passes_loaded_experts_to_the_agent(tmp_path: Path) -> None:
    """Whatever load_enabled_experts returns must reach the agent's config."""
    with (
        patch("openscientist.orchestrator.discovery.get_agent") as mock_get_agent,
        patch(
            "openscientist.orchestrator.discovery.load_enabled_experts",
            new=AsyncMock(return_value=_canned()),
        ),
    ):
        await _build_agent_executor(job_dir=tmp_path, data_file=None)

    experts = mock_get_agent.call_args[0][0].experts
    assert experts is not None
    assert set(experts) == {"alpha", "beta"}
    assert experts["alpha"].description == "alpha agent"


@pytest.mark.asyncio
async def test_discovery_handles_empty_expert_set(tmp_path: Path) -> None:
    """An empty loader result stays an empty mapping, not None."""
    with (
        patch("openscientist.orchestrator.discovery.get_agent") as mock_get_agent,
        patch(
            "openscientist.orchestrator.discovery.load_enabled_experts",
            new=AsyncMock(return_value={}),
        ),
    ):
        await _build_agent_executor(job_dir=tmp_path, data_file=None)

    assert mock_get_agent.call_args[0][0].experts == {}


@pytest.mark.asyncio
async def test_discovery_loader_is_invoked_with_a_session(tmp_path: Path) -> None:
    """The loader must be called with exactly one positional session arg."""
    spy = AsyncMock(return_value={})
    with (
        patch("openscientist.orchestrator.discovery.get_agent"),
        patch("openscientist.orchestrator.discovery.load_enabled_experts", new=spy),
    ):
        await _build_agent_executor(job_dir=tmp_path, data_file=None)

    assert spy.await_count == 1
    assert spy.await_args is not None
    args, kwargs = spy.await_args
    assert len(args) == 1
    assert hasattr(args[0], "execute")
    assert kwargs == {}


@pytest.mark.asyncio
async def test_an_unreadable_expert_catalog_does_not_fail_the_run(tmp_path: Path) -> None:
    """A catalog that cannot be read costs the run its roster, not the run."""
    with (
        patch("openscientist.orchestrator.discovery.get_agent") as mock_get_agent,
        patch(
            "openscientist.orchestrator.discovery.load_enabled_experts",
            new=AsyncMock(side_effect=RuntimeError("catalog down")),
        ),
    ):
        await _build_agent_executor(job_dir=tmp_path, data_file=None)

    assert mock_get_agent.call_args[0][0].experts == {}


@pytest.mark.asyncio
async def test_explicit_experts_skip_the_catalog(tmp_path: Path) -> None:
    """An explicit roster is used as given and reads no catalog."""
    spy = AsyncMock(return_value={})
    with (
        patch("openscientist.orchestrator.discovery.get_agent") as mock_get_agent,
        patch("openscientist.orchestrator.discovery.load_enabled_experts", new=spy),
    ):
        await _build_agent_executor(job_dir=tmp_path, data_file=None, experts=_canned())

    assert spy.await_count == 0
    assert set(mock_get_agent.call_args[0][0].experts) == {"alpha", "beta"}

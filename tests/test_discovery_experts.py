"""Tests that the discovery executor-construction path loads experts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import AgentDefinition

from openscientist.agent.sdk_executor import SDKAgentExecutor
from openscientist.orchestrator.discovery import _build_agent_executor


@pytest.mark.asyncio
async def test_discovery_passes_loaded_experts_to_executor(tmp_path: Path) -> None:
    """Whatever load_enabled_experts returns must end up on the executor."""
    canned = {
        "alpha": AgentDefinition(description="alpha agent", prompt="you are alpha"),
        "beta": AgentDefinition(description="beta agent", prompt="you are beta"),
    }
    with patch(
        "openscientist.orchestrator.discovery.load_enabled_experts",
        new=AsyncMock(return_value=canned),
    ):
        executor = await _build_agent_executor(
            job_dir=tmp_path,
            data_file=None,
        )

    assert isinstance(executor, SDKAgentExecutor)
    assert executor._experts is not None
    assert set(executor._experts.keys()) == {"alpha", "beta"}
    assert executor._experts["alpha"].description == "alpha agent"


@pytest.mark.asyncio
async def test_discovery_handles_empty_expert_set(tmp_path: Path) -> None:
    """An empty loader result yields an empty dict on the executor, not None."""
    with patch(
        "openscientist.orchestrator.discovery.load_enabled_experts",
        new=AsyncMock(return_value={}),
    ):
        executor = await _build_agent_executor(
            job_dir=tmp_path,
            data_file=None,
        )

    assert isinstance(executor, SDKAgentExecutor)
    assert executor._experts == {}


@pytest.mark.asyncio
async def test_discovery_loader_is_invoked_with_a_session(tmp_path: Path) -> None:
    """The loader must be called with exactly one positional session arg."""
    spy = AsyncMock(return_value={})
    with patch(
        "openscientist.orchestrator.discovery.load_enabled_experts",
        new=spy,
    ):
        await _build_agent_executor(
            job_dir=tmp_path,
            data_file=None,
        )

    assert spy.await_count == 1
    assert spy.await_args is not None
    args, kwargs = spy.await_args
    assert len(args) == 1
    assert hasattr(args[0], "execute")
    assert kwargs == {}

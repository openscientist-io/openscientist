"""Tests that the discovery agent-construction path loads experts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import AgentDefinition

from openscientist.agent.base import IterationResult, TurnOutcome
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.discovery import (
    _append_log,
    _build_agent_executor,
    _record_subagent_delegations,
)


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


def _result(log: tuple[str, ...]) -> IterationResult:
    """A completed turn reporting the given delegation log."""
    return IterationResult(
        outcome=TurnOutcome.COMPLETED,
        output="out",
        tool_calls=len(log),
        transcript=[],
        subagent_calls=len(log),
        subagent_names=frozenset(log),
        subagent_log=log,
    )


def test_every_delegation_becomes_one_analysis_entry() -> None:
    """A repeated expert is logged once per call, so the UI count matches."""
    ks = MagicMock()
    with (
        patch.object(KnowledgeState, "load_from_database_sync", return_value=ks),
        patch.object(KnowledgeState, "save_to_database_sync"),
    ):
        result = _result(("alpha", "beta", "alpha"))
        _record_subagent_delegations("job-1", result, list(result.subagent_log))

    assert [c.kwargs["description"] for c in ks.log_analysis.call_args_list] == [
        "Delegated to alpha",
        "Delegated to beta",
        "Delegated to alpha",
    ]
    assert {c.kwargs["action"] for c in ks.log_analysis.call_args_list} == {"delegate_to_expert"}


def test_a_turn_without_delegations_does_not_touch_the_knowledge_state() -> None:
    """No delegation means no load and no save, so no needless write."""
    with patch.object(KnowledgeState, "load_from_database_sync") as load:
        result = _result(())
        _record_subagent_delegations("job-1", result, list(result.subagent_log))

    load.assert_not_called()


def test_the_iteration_log_reports_delegations(tmp_path: Path) -> None:
    """The provenance log names how many delegations ran and to whom."""
    log_file = tmp_path / "iterations.log"
    _append_log(
        log_file,
        1,
        "prompt",
        "output",
        3,
        subagent_calls=3,
        subagent_names=frozenset({"beta", "alpha"}),
        write=True,
    )

    assert "Subagent delegations: 3 (alpha, beta)" in log_file.read_text()


def test_the_iteration_log_stays_silent_without_delegations(tmp_path: Path) -> None:
    """A turn that never delegated must not claim a delegation line."""
    log_file = tmp_path / "iterations.log"
    _append_log(log_file, 1, "prompt", "output", 2, write=True)

    assert "Subagent delegations" not in log_file.read_text()

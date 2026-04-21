"""Tests for SDKAgentExecutor expert-subagent wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk.types import AgentDefinition

from openscientist.agent.sdk_executor import SDKAgentExecutor


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    """Provide a temp job directory usable as cwd for the executor."""
    d = tmp_path / "job-xxxx"
    d.mkdir()
    return d


def _make_executor(job_dir: Path, **kwargs: object) -> SDKAgentExecutor:
    """Construct an SDKAgentExecutor with minimal arguments."""
    return SDKAgentExecutor(
        job_dir=job_dir,
        data_file=None,
        system_prompt="test-prompt",
        **kwargs,  # type: ignore[arg-type]
    )


def test_executor_accepts_none_experts(job_dir: Path) -> None:
    """Explicitly passing experts=None yields options.agents is None."""
    executor = _make_executor(job_dir, experts=None)
    options = executor._build_options()
    assert options.agents is None


def test_executor_accepts_empty_experts_dict(job_dir: Path) -> None:
    """Passing experts={} yields options.agents == {} (not None)."""
    executor = _make_executor(job_dir, experts={})
    options = executor._build_options()
    assert options.agents == {}


def test_executor_passes_populated_experts(job_dir: Path) -> None:
    """A populated experts dict reaches options.agents unchanged."""
    experts = {
        "foo": AgentDefinition(
            description="Foo expert",
            prompt="You are foo.",
        ),
        "bar": AgentDefinition(
            description="Bar expert",
            prompt="You are bar.",
            tools=["Read"],
            model="sonnet",
        ),
    }
    executor = _make_executor(job_dir, experts=experts)
    options = executor._build_options()

    assert options.agents is not None
    assert set(options.agents.keys()) == {"foo", "bar"}
    assert options.agents["foo"].description == "Foo expert"
    assert options.agents["bar"].tools == ["Read"]
    assert options.agents["bar"].model == "sonnet"


def test_executor_experts_default_is_none(job_dir: Path) -> None:
    """Omitting the experts kwarg defaults to None (backward compatible)."""
    executor = _make_executor(job_dir)
    options = executor._build_options()
    assert options.agents is None


def test_executor_experts_does_not_affect_other_options(job_dir: Path) -> None:
    """Passing experts must not change system_prompt, cwd, or mcp_servers."""
    experts = {"e": AgentDefinition(description="d", prompt="p")}
    with_experts = _make_executor(job_dir, experts=experts)
    without_experts = _make_executor(job_dir)

    opts_with = with_experts._build_options()
    opts_without = without_experts._build_options()

    assert opts_with.system_prompt == opts_without.system_prompt
    assert opts_with.cwd == opts_without.cwd
    assert opts_with.model == opts_without.model
    assert isinstance(opts_with.mcp_servers, dict)
    assert isinstance(opts_without.mcp_servers, dict)
    assert set(opts_with.mcp_servers.keys()) == set(opts_without.mcp_servers.keys())


def test_executor_experts_are_immutable_after_construction(job_dir: Path) -> None:
    """Mutating the passed-in dict must not change the executor's view."""
    experts = {"one": AgentDefinition(description="d1", prompt="p1")}
    executor = _make_executor(job_dir, experts=experts)

    experts["two"] = AgentDefinition(description="d2", prompt="p2")
    experts.pop("one")

    options = executor._build_options()
    assert options.agents is not None
    assert set(options.agents.keys()) == {"one"}

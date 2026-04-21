"""Tests that the chat executor-construction path loads experts."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import AgentDefinition

from openscientist.agent.sdk_executor import SDKAgentExecutor
from openscientist.job_chat import _build_chat_executor


@pytest.mark.asyncio
async def test_chat_passes_loaded_experts_to_executor(tmp_path: Path) -> None:
    """Whatever load_enabled_experts returns must end up on the chat executor."""
    canned = {
        "chat-expert": AgentDefinition(
            description="a chat-facing expert",
            prompt="you help users discuss findings",
        ),
    }
    with patch(
        "openscientist.job_chat.load_enabled_experts",
        new=AsyncMock(return_value=canned),
    ):
        executor = await _build_chat_executor(
            job_dir=tmp_path,
            system_prompt="test chat prompt",
        )

    assert isinstance(executor, SDKAgentExecutor)
    assert executor._experts is not None
    assert set(executor._experts.keys()) == {"chat-expert"}


@pytest.mark.asyncio
async def test_chat_handles_empty_expert_set(tmp_path: Path) -> None:
    """An empty loader result yields an empty dict on the chat executor."""
    with patch(
        "openscientist.job_chat.load_enabled_experts",
        new=AsyncMock(return_value={}),
    ):
        executor = await _build_chat_executor(
            job_dir=tmp_path,
            system_prompt="test chat prompt",
        )

    assert isinstance(executor, SDKAgentExecutor)
    assert executor._experts == {}


@pytest.mark.asyncio
async def test_chat_loader_is_invoked_with_a_session(tmp_path: Path) -> None:
    """The loader must be called with exactly one positional session arg."""
    spy = AsyncMock(return_value={})
    with patch(
        "openscientist.job_chat.load_enabled_experts",
        new=spy,
    ):
        await _build_chat_executor(
            job_dir=tmp_path,
            system_prompt="test chat prompt",
        )

    assert spy.await_count == 1
    assert spy.await_args is not None
    args, kwargs = spy.await_args
    assert len(args) == 1
    assert hasattr(args[0], "execute")
    assert kwargs == {}


@pytest.mark.asyncio
async def test_chat_degrades_gracefully_when_expert_loading_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Broken admin DB degrades to empty experts, not a crash."""
    with patch(
        "openscientist.job_chat.load_enabled_experts",
        new=AsyncMock(side_effect=RuntimeError("admin DB unreachable")),
    ):
        with caplog.at_level(logging.WARNING):
            executor = await _build_chat_executor(
                job_dir=tmp_path,
                system_prompt="test chat prompt",
            )

    assert isinstance(executor, SDKAgentExecutor)
    assert executor._experts == {}
    assert any(
        "admin DB unreachable" in record.message or "Failed to load experts" in record.message
        for record in caplog.records
    )

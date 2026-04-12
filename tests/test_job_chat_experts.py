"""Tests that a chat turn carries the expert roster to its agent."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import AgentDefinition

from openscientist.agent.expert_loader import experts_from_payload, experts_to_payload
from openscientist.job_chat import _CHAT_REQUEST_FILE, _load_chat_experts, run_chat_turn_async


def _canned() -> dict[str, AgentDefinition]:
    return {
        "chat-expert": AgentDefinition(
            description="a chat-facing expert",
            prompt="you help users discuss findings",
            tools=["Read"],
            model="sonnet",
        ),
    }


@pytest.mark.asyncio
async def test_chat_loads_the_enabled_roster() -> None:
    """Whatever load_enabled_experts returns is the chat turn's roster."""
    with patch(
        "openscientist.job_chat.load_enabled_experts",
        new=AsyncMock(return_value=_canned()),
    ):
        experts = await _load_chat_experts()

    assert set(experts) == {"chat-expert"}


@pytest.mark.asyncio
async def test_chat_loader_is_invoked_with_a_session() -> None:
    """The loader must be called with exactly one positional session arg."""
    spy = AsyncMock(return_value={})
    with patch("openscientist.job_chat.load_enabled_experts", new=spy):
        await _load_chat_experts()

    assert spy.await_count == 1
    assert spy.await_args is not None
    args, kwargs = spy.await_args
    assert len(args) == 1
    assert hasattr(args[0], "execute")
    assert kwargs == {}


@pytest.mark.asyncio
async def test_chat_degrades_gracefully_when_expert_loading_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable catalog degrades to an empty roster, not a crash."""
    with patch(
        "openscientist.job_chat.load_enabled_experts",
        new=AsyncMock(side_effect=RuntimeError("admin DB unreachable")),
    ):
        with caplog.at_level(logging.WARNING):
            experts = await _load_chat_experts()

    assert experts == {}
    assert any("Failed to load experts" in record.message for record in caplog.records)


def test_roster_survives_the_request_round_trip() -> None:
    """The roster crosses to the container as JSON, so it must rebuild intact."""
    rebuilt = experts_from_payload(json.loads(json.dumps(experts_to_payload(_canned()))))

    assert set(rebuilt) == {"chat-expert"}
    definition = rebuilt["chat-expert"]
    assert definition.description == "a chat-facing expert"
    assert definition.prompt == "you help users discuss findings"
    assert definition.tools == ["Read"]
    assert definition.model == "sonnet"


def test_a_model_the_sdk_rejects_is_dropped() -> None:
    """A payload naming an unsupported model loses that expert, not the turn."""
    payload = experts_to_payload(_canned())
    payload["chat-expert"]["model"] = "gpt-4"

    assert experts_from_payload(payload) == {}


@pytest.mark.asyncio
async def test_container_registers_the_roster_it_is_handed(tmp_path: Path) -> None:
    """run_chat_turn_async puts the request's roster on the agent config."""
    (tmp_path / _CHAT_REQUEST_FILE).write_text(
        json.dumps(
            {
                "system_prompt": "sp",
                "model_override": None,
                "prompt": "hello",
                "experts": experts_to_payload(_canned()),
            }
        )
    )

    with (
        patch("openscientist.agent.factory.build_agent") as mock_build_agent,
        patch("openscientist.providers.get_provider"),
    ):
        mock_build_agent.return_value.run_iteration = AsyncMock(
            return_value=type("R", (), {"success": True, "output": "hi", "error": ""})()
        )
        mock_build_agent.return_value.shutdown = AsyncMock()
        await run_chat_turn_async(tmp_path)

    config = mock_build_agent.call_args[0][0]
    assert config.experts is not None
    assert set(config.experts) == {"chat-expert"}

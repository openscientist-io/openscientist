"""Tests for the agent factory: provider registry and family dispatch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from openscientist.agent.base import AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.factory import (
    _instantiate_provider,
    get_agent,
)
from openscientist.providers import provider_class, provider_ids
from openscientist.providers.anthropic import AnthropicProvider
from openscientist.providers.base import CostInfo, Provider
from tests.helpers import StubClaudeProvider as _ClaudeStub


class _FamilylessProvider(Provider):
    """A provider that implements no agent compatibility family."""

    @property
    def id(self) -> str:
        return "familyless"

    @property
    def display_name(self) -> str:
        return "Familyless"

    def validate_required_config(self) -> list[str]:
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
        )


def test_registry_maps_known_ids() -> None:
    assert provider_class("anthropic") is AnthropicProvider
    assert set(provider_ids()) == {
        "anthropic",
        "cborg",
        "vertex",
        "bedrock",
        "foundry",
        "openai",
        "azure-openai",
        "ollama",
    }


def test_instantiate_provider_unknown_id_raises() -> None:
    with pytest.raises(ValueError, match="Unknown provider 'nope'"):
        _instantiate_provider("nope")


def test_get_agent_returns_claude_code_agent(tmp_path: Path) -> None:
    provider = _ClaudeStub()
    config = AgentConfig(job_dir=tmp_path)
    with patch("openscientist.agent.factory._instantiate_provider", return_value=provider):
        agent = get_agent(config)

    assert isinstance(agent, ClaudeCodeAgent)
    assert agent.config is config
    assert agent.provider is provider


def test_get_agent_returns_codex_agent(tmp_path: Path) -> None:
    from openscientist.agent.codex_agent import CodexAgent
    from tests.helpers import StubCodexProvider

    provider = StubCodexProvider()
    config = AgentConfig(job_dir=tmp_path)
    with patch("openscientist.agent.factory._instantiate_provider", return_value=provider):
        agent = get_agent(config)

    assert isinstance(agent, CodexAgent)
    assert agent.provider is provider


def test_openai_registered_for_codex() -> None:
    from openscientist.providers.openai import OpenAIDirectProvider

    assert provider_class("openai") is OpenAIDirectProvider


def test_factory_imports_without_codex_sdk() -> None:
    """The factory (and thus discovery) must import even where the codex SDK
    is absent, e.g. the web image. The codex SDK is imported lazily on the
    codex dispatch branch only. Guards a real regression that broke the
    container build."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import builtins
        _orig = builtins.__import__
        def _blocked(name, *a, **k):
            if name == "openai_codex" or name.startswith("openai_codex."):
                raise ModuleNotFoundError("blocked for test")
            return _orig(name, *a, **k)
        builtins.__import__ = _blocked
        import openscientist.agent.factory  # noqa: F401
        print("IMPORT_OK")
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_get_agent_rejects_provider_without_family(tmp_path: Path) -> None:
    with patch(
        "openscientist.agent.factory._instantiate_provider",
        return_value=_FamilylessProvider(),
    ):
        with pytest.raises(ValueError, match="compatibility family"):
            get_agent(AgentConfig(job_dir=tmp_path))

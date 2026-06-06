"""Tests for the agent factory's air-gap dispatch.

When ``settings.airgap.enabled`` is True, the factory must select
:class:`AirgapCodexAgent` for ``CodexCompatible`` providers and refuse
``ClaudeCompatible`` providers with a clear error (PR-1 ships only the
Codex air-gap path; the Claude variant lands in a follow-up).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist.agent.base import AgentConfig
from openscientist.agent.factory import get_agent
from tests.helpers import StubClaudeProvider, StubCodexProvider


def _settings(airgap_enabled: bool) -> SimpleNamespace:
    """Stand-in for :class:`Settings` exposing only what the factory reads.

    The factory only touches ``provider.provider_id`` (via
    ``_instantiate_provider``, which we patch separately) and ``airgap.enabled``
    — so this stub is all we need.
    """
    return SimpleNamespace(
        provider=SimpleNamespace(provider_id="stub"),
        airgap=SimpleNamespace(enabled=airgap_enabled),
    )


class TestAirgapDisabled:
    """When ``airgap.enabled`` is False, the factory's behavior is unchanged
    — same baseline tests as ``test_factory.py`` but threaded through the
    new conditional."""

    def test_claude_returns_plain_claude_code_agent(self, tmp_path: Path) -> None:
        from openscientist.agent.claude_code_agent import ClaudeCodeAgent

        provider = StubClaudeProvider()
        with (
            patch("openscientist.agent.factory._instantiate_provider", return_value=provider),
            patch(
                "openscientist.agent.factory.get_settings",
                return_value=_settings(airgap_enabled=False),
            ),
        ):
            agent = get_agent(AgentConfig(job_dir=tmp_path))
        assert isinstance(agent, ClaudeCodeAgent)

    def test_codex_returns_plain_codex_agent(self, tmp_path: Path) -> None:
        from openscientist.agent.codex_agent import CodexAgent

        provider = StubCodexProvider()
        with (
            patch("openscientist.agent.factory._instantiate_provider", return_value=provider),
            patch(
                "openscientist.agent.factory.get_settings",
                return_value=_settings(airgap_enabled=False),
            ),
        ):
            agent = get_agent(AgentConfig(job_dir=tmp_path))
        assert isinstance(agent, CodexAgent)


class TestAirgapEnabled:
    def test_codex_returns_airgap_subclass(self, tmp_path: Path) -> None:
        from openscientist.agent.codex_agent import CodexAgent
        from openscientist.airgap.codex_agent import AirgapCodexAgent

        provider = StubCodexProvider()
        with (
            patch("openscientist.agent.factory._instantiate_provider", return_value=provider),
            patch(
                "openscientist.agent.factory.get_settings",
                return_value=_settings(airgap_enabled=True),
            ),
        ):
            agent = get_agent(AgentConfig(job_dir=tmp_path))
        # Must be the airgap subclass, not the base.
        assert isinstance(agent, AirgapCodexAgent)
        # Sanity: still a CodexAgent for downstream isinstance checks.
        assert isinstance(agent, CodexAgent)
        assert agent.provider is provider

    def test_claude_provider_refused_with_clear_error(self, tmp_path: Path) -> None:
        # PR-1 ships only AirgapCodexAgent. ClaudeCompatible providers in
        # airgap mode must fail fast and explain why, not silently run the
        # un-hardened path.
        provider = StubClaudeProvider()
        with (
            patch("openscientist.agent.factory._instantiate_provider", return_value=provider),
            patch(
                "openscientist.agent.factory.get_settings",
                return_value=_settings(airgap_enabled=True),
            ),
        ):
            with pytest.raises(ValueError, match="Air-gap mode is enabled"):
                get_agent(AgentConfig(job_dir=tmp_path))

    def test_claude_error_names_the_followup(self, tmp_path: Path) -> None:
        # The error must point the operator at the resolution (PR-2 follow-up
        # OR switch to a Codex provider OR disable airgap), not just say
        # 'unsupported'.
        provider = StubClaudeProvider()
        with (
            patch("openscientist.agent.factory._instantiate_provider", return_value=provider),
            patch(
                "openscientist.agent.factory.get_settings",
                return_value=_settings(airgap_enabled=True),
            ),
        ):
            with pytest.raises(ValueError) as excinfo:
                get_agent(AgentConfig(job_dir=tmp_path))
        msg = str(excinfo.value)
        assert "OPENSCIENTIST_AIR_GAPPED" in msg
        assert "Codex-compatible" in msg

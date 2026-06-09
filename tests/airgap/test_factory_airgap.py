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


def _settings(
    airgap_enabled: bool,
    *,
    llm_addr: str = "10.0.0.5:8443",
    pubmed_addr: str = "10.0.0.6:9000",
) -> SimpleNamespace:
    """Stand-in for :class:`Settings` exposing only what the factory reads.

    The factory touches ``provider.provider_id`` (via ``_instantiate_provider``,
    which we patch separately), ``airgap.enabled``, and — when airgap is on —
    ``airgap.llm_addr`` + ``airgap.pubmed_addr`` to build the egress
    allowlist for ``validate_provider_for_airgap``.
    """
    return SimpleNamespace(
        provider=SimpleNamespace(
            provider_id="stub",
            # The codex providers' allowlisted URL fields the egress
            # registry reads. The StubCodexProvider's id is "stub-codex"
            # which isn't in the egress registry; tests below patch the
            # validate_provider_for_airgap call accordingly.
            ollama_base_url="http://10.0.0.5:8443/v1",
            anthropic_base_url=None,
            anthropic_foundry_base_url=None,
            anthropic_foundry_resource=None,
            azure_openai_resource=None,
            airgap_bedrock_endpoint=None,
            airgap_vertex_endpoint=None,
        ),
        airgap=SimpleNamespace(
            enabled=airgap_enabled,
            llm_addr=llm_addr,
            pubmed_addr=pubmed_addr,
        ),
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
            # StubCodexProvider's id 'stub-codex' isn't in the egress registry;
            # the egress check is tested separately in test_egress_registry.py.
            # Here we patch it to assert the factory CALLS it (Review-6 wiring
            # fix) and to keep this test focused on dispatch.
            patch(
                "openscientist.airgap.egress_registry.validate_provider_for_airgap",
                return_value={("10.0.0.5", 8443)},
            ) as mock_validate,
        ):
            agent = get_agent(AgentConfig(job_dir=tmp_path))
        # Codex Review-6 wiring: the factory must call validate_provider_for_airgap.
        mock_validate.assert_called_once()
        called_args = mock_validate.call_args
        assert called_args.args[0] == "stub-codex"  # provider id
        # Must be the airgap subclass, not the base.
        assert isinstance(agent, AirgapCodexAgent)
        # Sanity: still a CodexAgent for downstream isinstance checks.
        assert isinstance(agent, CodexAgent)
        assert agent.provider is provider

    def test_codex_refused_when_egress_validation_fails(self, tmp_path: Path) -> None:
        # The complement to the above — when validate_provider_for_airgap
        # raises, the factory must NOT instantiate AirgapCodexAgent.
        from openscientist.airgap.egress_registry import AirGapPolicyError

        provider = StubCodexProvider()
        with (
            patch("openscientist.agent.factory._instantiate_provider", return_value=provider),
            patch(
                "openscientist.agent.factory.get_settings",
                return_value=_settings(airgap_enabled=True),
            ),
            patch(
                "openscientist.airgap.egress_registry.validate_provider_for_airgap",
                side_effect=AirGapPolicyError(
                    "Provider 'stub-codex' would reach external endpoint"
                ),
            ),
        ):
            with pytest.raises(AirGapPolicyError, match="would reach"):
                get_agent(AgentConfig(job_dir=tmp_path))

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
        # 'unsupported'. After PR #195 the message names ollama as the
        # validated local-model path.
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
        # PR #195 / RFC §7.4 integration: ollama is now a named alternative.
        assert "ollama" in msg
        assert "gpt-oss-120b" in msg

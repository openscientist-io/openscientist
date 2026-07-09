"""Tests for :class:`openscientist.airgap.claude_code_agent.AirgapClaudeCodeAgent`.

The three overrides are independently exercised against the base class's
behavior to catch any regression that would re-open one of the exfiltration
surfaces in RFC §10.3 / §12.1.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist.agent.base import AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.airgap.claude_code_agent import AirgapClaudeCodeAgent
from openscientist.airgap.mcp_filter import CLAUDE_BUILTINS_NETWORK


def _fake_provider(provider_id: str = "anthropic", sdk_env: dict[str, str] | None = None):
    """Stand-in for a :class:`ClaudeCompatible` provider.

    Only the attributes the agent helpers actually touch are populated; the
    real provider's other behavior is irrelevant here.
    """
    return SimpleNamespace(
        id=provider_id,
        claude_model_name=lambda: "test-model",
        claude_sdk_env=lambda: dict(sdk_env or {}),
    )


def _agent_config(tmp_path: Path) -> AgentConfig:
    job_dir = tmp_path / "test-job-123"
    job_dir.mkdir()
    return AgentConfig(
        job_dir=job_dir,
        system_prompt="test prompt",
        use_hypotheses=True,
        data_file=None,
        data_files=(),
    )


@pytest.fixture
def airgap_agent(tmp_path: Path) -> AirgapClaudeCodeAgent:
    return AirgapClaudeCodeAgent(_agent_config(tmp_path), _fake_provider())  # type: ignore[arg-type]


@pytest.fixture
def base_agent(tmp_path: Path) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(_agent_config(tmp_path), _fake_provider())  # type: ignore[arg-type]


def _settings_stub(*, airgap_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(airgap=SimpleNamespace(enabled=airgap_enabled))


# --------------------------------------------------------- _build_options (built-in gating)


class TestBuiltinToolGating:
    """RFC §10.3: network-capable SDK built-ins must be disabled under airgap."""

    def test_base_class_does_not_disable_builtins(self, base_agent: ClaudeCodeAgent) -> None:
        options = base_agent._build_options()
        assert not options.disallowed_tools

    def test_airgap_disables_network_builtins(self, airgap_agent: AirgapClaudeCodeAgent) -> None:
        with patch(
            "openscientist.airgap.claude_code_agent.get_settings",
            return_value=_settings_stub(airgap_enabled=True),
        ):
            options = airgap_agent._build_options()
        assert set(options.disallowed_tools) == set(CLAUDE_BUILTINS_NETWORK)
        assert "WebFetch" in options.disallowed_tools
        assert "WebSearch" in options.disallowed_tools


# --------------------------------------------------------- _build_options (auth env routing)


class TestOptionsEnvFiltering:
    """RFC §12.1: provider auth env must reach the SDK CLI only via the
    filtered ``options.env``, never via a process-wide ``os.environ`` write."""

    def test_active_provider_auth_reaches_options_env(self, tmp_path: Path) -> None:
        agent = AirgapClaudeCodeAgent(
            _agent_config(tmp_path),
            _fake_provider(sdk_env={"ANTHROPIC_API_KEY": "active-secret"}),
        )
        with (
            patch("os.environ", {"PATH": "/bin"}),
            patch(
                "openscientist.airgap.claude_code_agent.get_settings",
                return_value=_settings_stub(airgap_enabled=True),
            ),
        ):
            options = agent._build_options()
        assert options.env["ANTHROPIC_API_KEY"] == "active-secret"

    def test_inactive_provider_creds_stripped_from_options_env(self, tmp_path: Path) -> None:
        agent = AirgapClaudeCodeAgent(
            _agent_config(tmp_path),
            _fake_provider(sdk_env={"ANTHROPIC_API_KEY": "active-secret"}),
        )
        polluted = {
            "PATH": "/bin",
            "OPENAI_API_KEY": "should-be-stripped",
            "AZURE_OPENAI_API_KEY": "should-be-stripped",
            "AWS_ACCESS_KEY_ID": "should-be-stripped",
            "GITHUB_TOKEN": "should-be-stripped",
        }
        with (
            patch.dict("os.environ", polluted, clear=True),
            patch(
                "openscientist.airgap.claude_code_agent.get_settings",
                return_value=_settings_stub(airgap_enabled=True),
            ),
        ):
            options = agent._build_options()
        for k in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "GITHUB_TOKEN"):
            assert k not in options.env, f"{k} reached options.env — env_allowlist bypassed"


# --------------------------------------------------------- _build_subprocess_env


class TestSubprocessEnvFiltering:
    """RFC §12.1: the MCP tools subprocess env must not carry inactive-provider creds."""

    def test_strips_inactive_provider_creds(self, airgap_agent: AirgapClaudeCodeAgent) -> None:
        polluted = {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "active-secret",
            "OPENAI_API_KEY": "should-be-stripped",
            "AZURE_OPENAI_API_KEY": "should-be-stripped",
            "AWS_ACCESS_KEY_ID": "should-be-stripped",
            "GITHUB_TOKEN": "should-be-stripped",
            "DATABASE_URL": "postgresql://user:pass@db/x",
        }
        with patch.dict("os.environ", polluted, clear=True):
            env = airgap_agent._build_subprocess_env()
        # Active provider's credential survives.
        assert env["ANTHROPIC_API_KEY"] == "active-secret"
        # All other provider creds + cross-cutting secrets are gone.
        assert "OPENAI_API_KEY" not in env
        assert "AZURE_OPENAI_API_KEY" not in env
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "GITHUB_TOKEN" not in env
        # DATABASE_URL is an operational necessity (RFC §12.1 TODO), same
        # exemption AirgapCodexAgent's _mcp_env relies on.
        assert env.get("DATABASE_URL") == "postgresql://user:pass@db/x"

    def test_overlay_still_applied(self, airgap_agent: AirgapClaudeCodeAgent) -> None:
        # The per-job overlay (job id, job dir, use_hypotheses) must still
        # land in the env — it's how the MCP server identifies the job.
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            env = airgap_agent._build_subprocess_env()
        assert env["OPENSCIENTIST_JOB_ID"] == airgap_agent._config.job_dir.name
        assert env["OPENSCIENTIST_JOB_DIR"] == str(airgap_agent._config.job_dir)
        assert env["OPENSCIENTIST_USE_HYPOTHESES"] == "1"

    def test_base_class_does_not_filter(self, base_agent: ClaudeCodeAgent) -> None:
        # Regression sentinel — if the base class starts filtering on its
        # own, this test catches it and we revisit the subclass design.
        polluted = {"PATH": "/bin", "OPENAI_API_KEY": "polluting-secret"}
        with patch.dict("os.environ", polluted, clear=True):
            env = base_agent._build_subprocess_env()
        assert env["OPENAI_API_KEY"] == "polluting-secret"


# --------------------------------------------------------- _apply_provider_env


class TestApplyProviderEnvNoop:
    """RFC §12.1: air-gap mode must not mutate the parent process environment.

    ``ClaudeAgentOptions.env`` is additive to ``os.environ`` for the SDK's
    CLI subprocess, so a process-wide write here would leak straight through
    the additive merge regardless of what ``_build_options`` passes.
    """

    def test_does_not_mutate_os_environ(self, airgap_agent: AirgapClaudeCodeAgent) -> None:
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            airgap_agent._apply_provider_env()
            import os

            assert dict(os.environ) == {"PATH": "/bin"}

    def test_base_class_mutates_os_environ(self, tmp_path: Path) -> None:
        # Regression sentinel — if the base class stops mutating os.environ
        # on its own, this test catches it and we revisit whether the
        # subclass override (and its rationale) is still needed.
        agent = ClaudeCodeAgent(
            _agent_config(tmp_path),
            _fake_provider(sdk_env={"ANTHROPIC_API_KEY": "leaks-into-parent-env"}),
        )
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            agent._apply_provider_env()
            import os

            assert os.environ.get("ANTHROPIC_API_KEY") == "leaks-into-parent-env", (
                "Base ClaudeCodeAgent stopped mutating os.environ in "
                "_apply_provider_env — if intentional, drop the "
                "AirgapClaudeCodeAgent no-op override and this sentinel."
            )

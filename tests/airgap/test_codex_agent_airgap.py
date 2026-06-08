"""Tests for :class:`openscientist.airgap.codex_agent.AirgapCodexAgent`.

The four overrides are independently exercised against the base class's
behavior to catch any regression that would re-open one of the four
exfiltration surfaces in RFC §12.2 / §8.2.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist.agent.base import AgentConfig
from openscientist.agent.codex_agent import CodexAgent
from openscientist.airgap.codex_agent import (
    _AIRGAP_CODEX_HOME_ROOT_DEFAULT,
    _AIRGAP_CODEX_HOME_ROOT_ENV,
    AirgapCodexAgent,
)


def _fake_provider(provider_id: str = "anthropic") -> SimpleNamespace:
    """Stand-in for a :class:`CodexCompatible` provider.

    Only the attributes the agent helpers actually touch are populated; the
    real provider's other behavior is irrelevant here.
    """
    return SimpleNamespace(
        id=provider_id,
        codex_model_name=lambda: "test-model",
        codex_model_provider_id=lambda: "test-provider",
        codex_config_overrides=lambda: [],
        codex_sdk_env=lambda: {},
    )


def _agent_config(tmp_path: Path) -> AgentConfig:
    job_dir = tmp_path / "test-job-123"
    job_dir.mkdir()
    return AgentConfig(
        job_dir=job_dir,
        system_prompt="test prompt",
        use_hypotheses=True,
        data_file=None,
        data_files=[],
    )


@pytest.fixture
def airgap_agent(tmp_path: Path) -> AirgapCodexAgent:
    return AirgapCodexAgent(_agent_config(tmp_path), _fake_provider())


@pytest.fixture
def base_agent(tmp_path: Path) -> CodexAgent:
    return CodexAgent(_agent_config(tmp_path), _fake_provider())


# --------------------------------------------------------- _codex_home


class TestCodexHomeRelocation:
    """RFC §12.2: the per-job ``CODEX_HOME`` must not land inside the export
    tree. Air-gap mode moves it to a tmpfs path."""

    def test_base_class_uses_job_dir(self, base_agent: CodexAgent) -> None:
        # Baseline — the un-overridden behavior puts it under the job_dir.
        assert base_agent._codex_home().is_relative_to(base_agent._config.job_dir)

    def test_airgap_relocates_outside_job_dir(self, airgap_agent: AirgapCodexAgent) -> None:
        home = airgap_agent._codex_home()
        # Must NOT be inside the job dir (would otherwise end up in the
        # exported artifact ZIP).
        assert not home.is_relative_to(airgap_agent._config.job_dir)

    def test_default_root(self, airgap_agent: AirgapCodexAgent) -> None:
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop(_AIRGAP_CODEX_HOME_ROOT_ENV, None)
            home = airgap_agent._codex_home()
        assert home.parent == _AIRGAP_CODEX_HOME_ROOT_DEFAULT

    def test_env_override(self, airgap_agent: AirgapCodexAgent, tmp_path: Path) -> None:
        override = tmp_path / "tmpfs-root"
        with patch.dict("os.environ", {_AIRGAP_CODEX_HOME_ROOT_ENV: str(override)}):
            home = airgap_agent._codex_home()
        assert home.parent == override

    def test_per_job_namespace(self, airgap_agent: AirgapCodexAgent) -> None:
        # The job_dir's basename is appended so two concurrent jobs don't
        # collide on the same tmpfs path.
        home = airgap_agent._codex_home()
        assert home.name == airgap_agent._config.job_dir.name


# --------------------------------------------------------- _mcp_env


class TestMcpEnvFiltering:
    """RFC §12.1: the MCP env must not carry inactive-provider creds."""

    def test_strips_inactive_provider_creds(self, airgap_agent: AirgapCodexAgent) -> None:
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
            env = airgap_agent._mcp_env()
        # Active provider's credential survives.
        assert env["ANTHROPIC_API_KEY"] == "active-secret"
        # All other provider creds + cross-cutting secrets are gone.
        assert "OPENAI_API_KEY" not in env
        assert "AZURE_OPENAI_API_KEY" not in env
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "GITHUB_TOKEN" not in env
        assert "DATABASE_URL" not in env

    def test_overlay_still_applied(self, airgap_agent: AirgapCodexAgent) -> None:
        # The per-job overlay (job id, job dir, use_hypotheses) must still
        # land in the env — it's how the MCP server identifies the job.
        with patch.dict("os.environ", {"PATH": "/bin"}, clear=True):
            env = airgap_agent._mcp_env()
        assert env["OPENSCIENTIST_JOB_ID"] == airgap_agent._config.job_dir.name
        assert env["OPENSCIENTIST_JOB_DIR"] == str(airgap_agent._config.job_dir)
        assert env["OPENSCIENTIST_USE_HYPOTHESES"] == "1"

    def test_base_class_does_not_filter(self, base_agent: CodexAgent) -> None:
        # Regression sentinel — if the base class starts filtering on its own,
        # this test catches it and we revisit the subclass design.
        polluted = {
            "PATH": "/bin",
            "OPENAI_API_KEY": "polluting-secret",
        }
        with patch.dict("os.environ", polluted, clear=True):
            env = base_agent._mcp_env()
        # Base class forwards everything.
        assert env["OPENAI_API_KEY"] == "polluting-secret"


# --------------------------------------------------------- _thread_options (removed after PR #195)
#
# The previous draft had a TestThreadOptionsHardening class that asserted
# AirgapCodexAgent overrode _thread_options() to set network_access_enabled=
# False + web_search_enabled=False on a ThreadOptions object. PR #195 swaps
# openai-codex-sdk for openai-codex, which:
#
#   - drops the ThreadOptions dataclass entirely (thread_start takes kwargs);
#   - gates web_search at the FORK level for non-OpenAI providers (Ollama,
#     BedrockOpenAI, AzureOpenAI — every CodexCompatible provider the air-gap
#     egress registry supports);
#   - has no network_access_enabled parameter — network policy lives at the
#     host firewall + Docker network configuration (RFC §6).
#
# So the four overrides shrink to three; nothing to test here. The fork-level
# web_search gate is the upstream's responsibility to assert; the network
# boundary is asserted by `tests/airgap/test_probes.py`.


# --------------------------------------------------------- _ensure_auth


class TestEnsureAuthNoop:
    """RFC §12.2: the air-gap agent must not touch the host's ``~/.codex/``.

    Auth is provisioned by the runner (host-mounted secret); the agent
    process has no business reading the host filesystem.
    """

    def test_no_copy_from_host(
        self,
        airgap_agent: AirgapCodexAgent,
        tmp_path: Path,
    ) -> None:
        # The subclass override is a no-op — no shutil.copy2 should run even
        # when the would-be source exists.
        with patch("shutil.copy2") as mock_copy:
            airgap_agent._ensure_auth()
        mock_copy.assert_not_called()

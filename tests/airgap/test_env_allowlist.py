"""Tests for :mod:`openscientist.airgap.env_allowlist`."""

from __future__ import annotations

import pytest

from openscientist.airgap.env_allowlist import (
    BASE_AIRGAP_ENV,
    PROVIDER_ENV_VARS,
    filtered_agent_env,
)

# --------------------------------------------------------- shape / consistency


class TestRegistryShape:
    """The provider key set must align with the egress registry's. Adding a
    provider to one without the other is the kind of skew RFC §12 wants to
    prevent."""

    def test_known_provider_keys(self) -> None:
        # Note Bedrock and Vertex are intentionally absent — see RFC §19 OQ#2.
        # When that resolves, this set updates here and the egress registry
        # unsupported entries flip.
        assert set(PROVIDER_ENV_VARS) == {
            "anthropic",
            "cborg",
            "openai",
            "azure-openai",
            "foundry",
            "ollama",  # PR #195
        }

    def test_base_env_has_no_credential_content(self) -> None:
        # Sanity: any var name suggesting "KEY", "TOKEN", or "SECRET" in the
        # base set is a smell. Process-runtime + model-selection only.
        for name in BASE_AIRGAP_ENV:
            assert "KEY" not in name, f"{name} smells like a credential in base set"
            assert "TOKEN" not in name, f"{name} smells like a credential in base set"
            assert "SECRET" not in name, f"{name} smells like a credential in base set"


# --------------------------------------------------------- filtered_agent_env


class TestFilteredAgentEnv:
    """The active-provider-only credential filter (RFC §12.1)."""

    def _full_env(self) -> dict[str, str]:
        """A realistic env carrying every provider's credentials (the
        pre-filter state that ``get_container_env_vars()`` produces today)."""
        return {
            # Base / process
            "PATH": "/usr/bin:/bin",
            "HOME": "/root",
            "USER": "root",
            "OPENSCIENTIST_PROVIDER": "anthropic",
            "OPENSCIENTIST_MODEL": "claude-opus-4-7",
            # Anthropic
            "ANTHROPIC_API_KEY": "sk-anthropic-secret",
            "ANTHROPIC_BASE_URL": "https://llm.internal:8443",
            # OpenAI
            "OPENAI_API_KEY": "sk-openai-secret",
            # Azure OpenAI
            "AZURE_OPENAI_API_KEY": "azure-secret",
            "AZURE_OPENAI_RESOURCE": "myaoai",
            # Foundry
            "ANTHROPIC_FOUNDRY_API_KEY": "foundry-secret",
            "ANTHROPIC_FOUNDRY_RESOURCE": "myfoundry",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.internal",
            # Bedrock
            "AWS_ACCESS_KEY_ID": "AKIA-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AWS_REGION": "us-east-1",
            # Vertex
            "ANTHROPIC_VERTEX_PROJECT_ID": "myproject",
            "GOOGLE_APPLICATION_CREDENTIALS": "/etc/gcp.json",
            # Cross-cutting secrets
            "GITHUB_TOKEN": "ghp-secret",
            "OPENSCIENTIST_SECRET_KEY": "master-secret",
            "DATABASE_URL": "postgresql://user:pass@db/x",
        }

    def test_active_provider_creds_preserved(self) -> None:
        filtered = filtered_agent_env(self._full_env(), "anthropic")
        assert filtered["ANTHROPIC_API_KEY"] == "sk-anthropic-secret"
        assert filtered["ANTHROPIC_BASE_URL"] == "https://llm.internal:8443"

    def test_inactive_provider_creds_stripped(self) -> None:
        filtered = filtered_agent_env(self._full_env(), "anthropic")
        # Every other provider's secrets must not appear.
        assert "OPENAI_API_KEY" not in filtered
        assert "AZURE_OPENAI_API_KEY" not in filtered
        assert "ANTHROPIC_FOUNDRY_API_KEY" not in filtered
        assert "AWS_ACCESS_KEY_ID" not in filtered
        assert "AWS_SECRET_ACCESS_KEY" not in filtered

    def test_cross_cutting_secrets_stripped(self) -> None:
        # GITHUB_TOKEN, master secret, full DB URL all stripped per §12.1.
        filtered = filtered_agent_env(self._full_env(), "anthropic")
        assert "GITHUB_TOKEN" not in filtered
        assert "OPENSCIENTIST_SECRET_KEY" not in filtered
        assert "DATABASE_URL" not in filtered

    def test_base_env_preserved(self) -> None:
        filtered = filtered_agent_env(self._full_env(), "anthropic")
        assert filtered["PATH"] == "/usr/bin:/bin"
        assert filtered["HOME"] == "/root"
        assert filtered["OPENSCIENTIST_MODEL"] == "claude-opus-4-7"

    @pytest.mark.parametrize(
        "active,expected_present,expected_absent",
        [
            (
                "openai",
                {"OPENAI_API_KEY"},
                {"ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY"},
            ),
            (
                "azure-openai",
                {"AZURE_OPENAI_API_KEY", "AZURE_OPENAI_RESOURCE"},
                {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"},
            ),
            (
                "foundry",
                {
                    "ANTHROPIC_FOUNDRY_API_KEY",
                    "ANTHROPIC_FOUNDRY_RESOURCE",
                    "ANTHROPIC_FOUNDRY_BASE_URL",
                },
                {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
            ),
            (
                "cborg",
                {"ANTHROPIC_BASE_URL"},
                {"OPENAI_API_KEY", "ANTHROPIC_FOUNDRY_API_KEY"},
            ),
        ],
    )
    def test_each_provider_filter(
        self,
        active: str,
        expected_present: set[str],
        expected_absent: set[str],
    ) -> None:
        filtered = filtered_agent_env(self._full_env(), active)
        for name in expected_present:
            assert name in filtered, f"{name} should pass through for {active}"
        for name in expected_absent:
            assert name not in filtered, f"{name} should be stripped for {active}"

    def test_unknown_provider_strips_all_creds(self) -> None:
        # No registry entry → no provider creds preserved. Fail-closed.
        filtered = filtered_agent_env(self._full_env(), "bedrock")
        # Base env still passes (provider id alone shouldn't break PATH etc.)
        assert "PATH" in filtered
        # But all credentials are stripped.
        for cred in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "ANTHROPIC_FOUNDRY_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ):
            assert cred not in filtered

    def test_empty_input(self) -> None:
        assert filtered_agent_env({}, "anthropic") == {}

    def test_does_not_mutate_input(self) -> None:
        original = self._full_env()
        snapshot = dict(original)
        filtered_agent_env(original, "anthropic")
        assert original == snapshot
        # Returned dict is a new object.
        result = filtered_agent_env(original, "anthropic")
        assert result is not original

"""Tests for Anthropic provider."""

import os
from unittest.mock import MagicMock, patch

import pytest

from openscientist.providers.anthropic import AnthropicProvider


class TestAnthropicProviderValidation:
    """Tests for Anthropic provider configuration validation."""

    def test_no_key_no_oauth_raises(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = None
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.omp_auth_host_path = None
        mock_settings.provider.model = "claude-sonnet-4-6"
        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="ANTHROPIC_API_KEY",
            ),
        ):
            AnthropicProvider()

    def test_api_key_present_no_error(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "sk-ant-test-key"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.model = "claude-sonnet-4-6"
        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            assert provider.display_name == "Anthropic"

    def test_oauth_present_no_error(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = None
        mock_settings.provider.claude_code_oauth_token = "oauth-token"
        mock_settings.provider.model = "claude-sonnet-4-6"
        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            assert provider.display_name == "Anthropic"

    def test_optional_no_model_warns(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "key"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.model = None
        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            # Provider should still initialize (warnings don't prevent init)
            assert provider.display_name == "Anthropic"


class TestAnthropicSetupEnvironment:
    """Tests for setup_environment()."""

    def test_api_key_mode(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "sk-test"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.model = "model"

        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings),
            patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                },
            ),
        ):
            provider = AnthropicProvider()
            provider.setup_environment()

            # Conflicting vars should be removed
            assert "CLAUDE_CODE_USE_VERTEX" not in os.environ
            assert "CLAUDE_CODE_USE_BEDROCK" not in os.environ
            assert "ANTHROPIC_VERTEX_PROJECT_ID" not in os.environ

    def test_oauth_mode_sets_token(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = None
        mock_settings.provider.claude_code_oauth_token = "my-oauth-token"
        mock_settings.provider.model = "model"

        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {}, clear=False),
        ):
            provider = AnthropicProvider()
            provider.setup_environment()

            assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "my-oauth-token"


def _mock_settings(
    *,
    api_key: str | None = "sk-ant-test-key",
    oauth: str | None = None,
    vault: str | None = None,
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.anthropic_api_key = api_key
    mock_settings.provider.claude_code_oauth_token = oauth
    mock_settings.provider.omp_auth_host_path = vault
    mock_settings.provider.model = model
    return mock_settings


class TestAnthropicClaudeCompatible:
    """Tests for the ClaudeCompatible family methods added in the migration."""

    def test_id_is_anthropic(self) -> None:
        with patch("openscientist.providers.anthropic.get_settings", return_value=_mock_settings()):
            assert AnthropicProvider().id == "anthropic"

    def test_display_name_is_anthropic(self) -> None:
        with patch("openscientist.providers.anthropic.get_settings", return_value=_mock_settings()):
            assert AnthropicProvider().display_name == "Anthropic"

    def test_is_claude_compatible_and_provider(self) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        with patch("openscientist.providers.anthropic.get_settings", return_value=_mock_settings()):
            provider = AnthropicProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok_with_api_key(self) -> None:
        assert AnthropicProvider.required_config_errors(_mock_settings().provider) == []

    def test_validate_required_config_error_when_unset(self) -> None:
        errors = AnthropicProvider.required_config_errors(
            _mock_settings(api_key=None, oauth=None).provider
        )
        assert len(errors) == 1
        assert "ANTHROPIC_API_KEY" in errors[0]

    def test_claude_sdk_env_api_key_mode(self) -> None:
        settings = _mock_settings(api_key="sk-ant-xyz", oauth=None)
        with patch("openscientist.providers.anthropic.get_settings", return_value=settings):
            provider = AnthropicProvider()
            assert provider.claude_sdk_env() == {"ANTHROPIC_API_KEY": "sk-ant-xyz"}

    def test_claude_sdk_env_oauth_mode(self) -> None:
        settings = _mock_settings(api_key=None, oauth="oauth-abc")
        with patch("openscientist.providers.anthropic.get_settings", return_value=settings):
            provider = AnthropicProvider()
            assert provider.claude_sdk_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-abc"}

    def test_api_key_and_oauth_token_together_are_refused(self) -> None:
        settings = _mock_settings(api_key="sk-ant-both", oauth="oauth-both")
        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=settings),
            pytest.raises(ValueError, match="ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN"),
        ):
            AnthropicProvider()

    def test_claude_sdk_env_empty_when_neither_set(self) -> None:
        # Construct with a valid config, then re-point settings to unset both.
        with patch("openscientist.providers.anthropic.get_settings", return_value=_mock_settings()):
            provider = AnthropicProvider()
        with patch(
            "openscientist.providers.anthropic.get_settings",
            return_value=_mock_settings(api_key=None, oauth=None),
        ):
            assert provider.claude_sdk_env() == {}

    def test_claude_model_name_uses_configured_model(self) -> None:
        settings = _mock_settings(model="claude-custom-model")
        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=settings),
            patch("openscientist.providers.base.get_settings", return_value=settings),
        ):
            assert AnthropicProvider().claude_model_name() == "claude-custom-model"


class TestAnthropicGetCostInfo:
    """Tests for get_cost_info()."""

    def test_returns_cost_info_with_none_spend(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "key"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.model = "model"

        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            cost = provider.get_cost_info()

        assert cost.provider_name == "Anthropic"
        assert cost.total_spend_usd is None
        assert cost.recent_spend_usd is None
        assert cost.recent_period_hours == 24

    def test_custom_lookback_hours(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "key"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.model = "model"

        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            cost = provider.get_cost_info(lookback_hours=48)

        assert cost.recent_period_hours == 48


def test_airgap_posture_is_unsupported_without_credentials() -> None:
    """Without any credential the provider cannot be routed through the proxy."""
    from types import SimpleNamespace

    from openscientist.providers.base import AirgapEgress

    posture = AnthropicProvider.airgap_posture(
        SimpleNamespace(
            anthropic_api_key=None,
            claude_code_oauth_token=None,
            omp_auth_host_path=None,
        )  # type: ignore[arg-type]
    )
    assert posture.mode is AirgapEgress.UNSUPPORTED

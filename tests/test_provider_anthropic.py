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
        mock_settings.provider.anthropic_model = "claude-sonnet-4-6"
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
        mock_settings.provider.anthropic_model = "claude-sonnet-4-6"
        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            assert provider.name == "Anthropic"

    def test_oauth_present_no_error(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = None
        mock_settings.provider.claude_code_oauth_token = "oauth-token"
        mock_settings.provider.anthropic_model = "claude-sonnet-4-6"
        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            assert provider.name == "Anthropic"

    def test_optional_no_model_warns(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "key"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.anthropic_model = None
        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            # Provider should still initialize (warnings don't prevent init)
            assert provider.name == "Anthropic"


class TestAnthropicSetupEnvironment:
    """Tests for setup_environment()."""

    def test_api_key_mode(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "sk-test"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.anthropic_model = "model"

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
        mock_settings.provider.anthropic_model = "model"

        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {}, clear=False),
        ):
            provider = AnthropicProvider()
            provider.setup_environment()

            assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "my-oauth-token"


class TestAnthropicGetCostInfo:
    """Tests for get_cost_info()."""

    def test_returns_cost_info_with_none_spend(self):
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_api_key = "key"
        mock_settings.provider.claude_code_oauth_token = None
        mock_settings.provider.anthropic_model = "model"

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
        mock_settings.provider.anthropic_model = "model"

        with patch("openscientist.providers.anthropic.get_settings", return_value=mock_settings):
            provider = AnthropicProvider()
            cost = provider.get_cost_info(lookback_hours=48)

        assert cost.recent_period_hours == 48

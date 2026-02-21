"""Tests for AWS Bedrock provider."""

import os
from unittest.mock import MagicMock, patch

import pytest

from shandy.providers.bedrock import BedrockProvider


class TestBedrockProviderValidation:
    """Tests for Bedrock provider configuration validation."""

    def test_no_region_raises(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = None
        mock_settings.provider.aws_access_key_id = "key"
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("shandy.providers.bedrock.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError, match="AWS_REGION"):
                BedrockProvider()

    def test_no_credentials_raises(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("shandy.providers.bedrock.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError, match="AWS credentials"):
                BedrockProvider()

    def test_valid_access_key_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = "AKIA..."
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("shandy.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert "bedrock" in provider.name.lower()

    def test_valid_profile_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-west-2"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = "my-profile"
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("shandy.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert provider.name == "AWS Bedrock"

    def test_valid_bearer_token_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "eu-west-1"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = "bearer-tok"
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("shandy.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert provider.name == "AWS Bedrock"


class TestBedrockSetupEnvironment:
    """Tests for setup_environment()."""

    def _make_provider(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = "AKIA..."
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"
        return mock_settings

    def test_sets_bedrock_flag(self):
        mock_settings = self._make_provider()

        with (
            patch("shandy.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {}, clear=False),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1"

    def test_clears_vertex_vars(self):
        mock_settings = self._make_provider()

        with (
            patch("shandy.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                },
            ),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert "CLAUDE_CODE_USE_VERTEX" not in os.environ
            assert "ANTHROPIC_VERTEX_PROJECT_ID" not in os.environ

    def test_clears_empty_vars(self):
        mock_settings = self._make_provider()

        with (
            patch("shandy.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(
                os.environ,
                {
                    "AWS_PROFILE": "",
                    "AWS_SESSION_TOKEN": "",
                    "ANTHROPIC_AUTH_TOKEN": "",
                },
            ),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            # Empty string vars should be removed
            assert "AWS_PROFILE" not in os.environ
            assert "AWS_SESSION_TOKEN" not in os.environ
            assert "ANTHROPIC_AUTH_TOKEN" not in os.environ

    def test_clears_api_key(self):
        mock_settings = self._make_provider()

        with (
            patch("shandy.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert "ANTHROPIC_API_KEY" not in os.environ

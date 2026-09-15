"""Tests for AWS Bedrock provider."""

import os
from unittest.mock import MagicMock, patch

import pytest

from openscientist.providers.bedrock import BedrockProvider


class TestBedrockProviderValidation:
    """Tests for Bedrock provider configuration validation."""

    def test_no_region_raises(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = None
        mock_settings.provider.aws_access_key_id = "key"
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="AWS_REGION",
            ),
        ):
            BedrockProvider()

    def test_no_credentials_raises(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="AWS credentials",
            ),
        ):
            BedrockProvider()

    def test_valid_access_key_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = "AKIA..."
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert "bedrock" in provider.display_name.lower()

    def test_valid_profile_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-west-2"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = "my-profile"
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert provider.display_name == "AWS Bedrock"

    def test_valid_bearer_token_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "eu-west-1"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = "bearer-tok"
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert provider.display_name == "AWS Bedrock"


class TestBedrockSetupEnvironment:
    """Tests for setup_environment()."""

    def _make_provider(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = "AKIA..."
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"
        return mock_settings

    def test_sets_bedrock_flag(self):
        mock_settings = self._make_provider()

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {}, clear=False),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1"

    def test_clears_vertex_vars(self):
        mock_settings = self._make_provider()

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
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
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
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
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert "ANTHROPIC_API_KEY" not in os.environ


def _mock_settings(
    *,
    region: str | None = "us-east-1",
    access_key: str | None = "AKIATEST",
    secret: str | None = "secret-key",
    profile: str | None = None,
    bearer: str | None = None,
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.aws_region = region
    mock_settings.provider.aws_access_key_id = access_key
    mock_settings.provider.aws_secret_access_key = secret
    mock_settings.provider.aws_profile = profile
    mock_settings.provider.aws_bearer_token_bedrock = bearer
    mock_settings.provider.model = model
    return mock_settings


class TestBedrockClaudeCompatible:
    """Tests for the ClaudeCompatible family methods."""

    def test_id_is_bedrock(self) -> None:
        with patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()):
            assert BedrockProvider().id == "bedrock"

    def test_display_name_is_aws_bedrock(self) -> None:
        with patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()):
            assert BedrockProvider().display_name == "AWS Bedrock"

    def test_is_claude_compatible_and_provider(self) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        with patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()):
            provider = BedrockProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok(self) -> None:
        assert BedrockProvider.required_config_errors(_mock_settings().provider) == []

    def test_validate_required_config_errors_when_unset(self) -> None:
        unset = _mock_settings(region=None, access_key=None, secret=None)
        errors = BedrockProvider.required_config_errors(unset.provider)
        assert len(errors) == 2
        assert any("AWS_REGION" in e for e in errors)
        assert any("credentials" in e.lower() for e in errors)

    def test_claude_sdk_env_access_key_mode(self) -> None:
        settings = _mock_settings(region="eu-west-1", access_key="AKIA9", secret="shh")
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            env = BedrockProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "eu-west-1",
            "AWS_ACCESS_KEY_ID": "AKIA9",
            "AWS_SECRET_ACCESS_KEY": "shh",
        }

    def test_claude_sdk_env_profile_mode(self) -> None:
        settings = _mock_settings(
            region="us-east-1", access_key=None, secret=None, profile="bedrock-prof"
        )
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            env = BedrockProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "us-east-1",
            "AWS_PROFILE": "bedrock-prof",
        }

    def test_claude_model_name_uses_configured_model(self) -> None:
        settings = _mock_settings(model="bedrock-custom")
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            assert BedrockProvider().claude_model_name() == "bedrock-custom"

    def test_claude_model_name_falls_back_to_bedrock_default(self) -> None:
        settings = _mock_settings(model=None)
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            assert (
                BedrockProvider().claude_model_name()
                == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
            )


class TestBedrockCostAndMessages:
    """Coverage for the cost + message paths (audit Priority-7)."""

    def _provider(self) -> BedrockProvider:
        with patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()):
            return BedrockProvider()

    def test_get_cost_info_returns_spend(self) -> None:
        provider = self._provider()
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "1.50"}}}]
        }
        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()),
            patch("boto3.client", return_value=ce),
        ):
            info = provider.get_cost_info(lookback_hours=24)
        assert info.provider_name == "AWS Bedrock"
        assert info.total_spend_usd == 1.5
        assert info.recent_spend_usd == 1.5

    def test_get_cost_info_handles_cost_explorer_error(self) -> None:
        provider = self._provider()
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = RuntimeError("access denied")
        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()),
            patch("boto3.client", return_value=ce),
        ):
            info = provider.get_cost_info()
        assert info.total_spend_usd is None
        assert info.recent_spend_usd is None
        assert "unavailable" in (info.data_lag_note or "").lower()

    async def test_send_message_uses_bedrock_client(self) -> None:
        provider = self._provider()
        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()),
            patch("anthropic.AnthropicBedrock") as mock_client_cls,
            patch(
                "openscientist.providers.bedrock.send_anthropic_message",
                return_value="hello",
            ) as mock_send,
        ):
            result = await provider.send_message([{"role": "user", "content": "hi"}])
        assert result == "hello"
        mock_client_cls.assert_called_once()
        assert mock_send.called

    async def test_send_message_with_tools_uses_bedrock_client(self) -> None:
        provider = self._provider()
        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()),
            patch("anthropic.AnthropicBedrock") as mock_client_cls,
            patch(
                "openscientist.providers.bedrock.send_anthropic_message_with_tools",
                return_value={"stop_reason": "end_turn", "content": []},
            ) as mock_send,
        ):
            result = await provider.send_message_with_tools(
                [{"role": "user", "content": "hi"}], tools=[]
            )
        assert result["stop_reason"] == "end_turn"
        mock_client_cls.assert_called_once()
        assert mock_send.called

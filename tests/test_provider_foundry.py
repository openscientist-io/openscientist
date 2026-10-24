"""Tests for Azure Foundry provider."""

from unittest.mock import MagicMock, patch

from openscientist.providers.foundry import FoundryProvider


def _mock_settings(
    *,
    resource: str | None = "my-foundry-res",
    base_url: str | None = None,
    api_key: str | None = "foundry-key",
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.anthropic_foundry_resource = resource
    mock_settings.provider.anthropic_foundry_base_url = base_url
    mock_settings.provider.anthropic_foundry_api_key = api_key
    mock_settings.provider.model = model
    return mock_settings


class TestFoundryClaudeCompatible:
    """Tests for the ClaudeCompatible family methods."""

    def test_id_is_foundry(self) -> None:
        with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
            assert FoundryProvider().id == "foundry"

    def test_display_name_is_azure_ai_foundry(self) -> None:
        with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
            assert FoundryProvider().display_name == "Azure AI Foundry"

    def test_is_claude_compatible_and_provider(self) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
            provider = FoundryProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok(self) -> None:
        assert FoundryProvider.required_config_errors(_mock_settings().provider) == []

    def test_validate_required_config_error_when_no_endpoint(self) -> None:
        no_endpoint = _mock_settings(resource=None, base_url=None, api_key="key")
        errors = FoundryProvider.required_config_errors(no_endpoint.provider)
        assert any("ANTHROPIC_FOUNDRY_RESOURCE" in e for e in errors)

    def test_claude_sdk_env_resource_mode(self) -> None:
        settings = _mock_settings(resource="res-a", base_url=None, api_key="k1")
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            env = FoundryProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_RESOURCE": "res-a",
            "ANTHROPIC_FOUNDRY_API_KEY": "k1",
        }

    def test_claude_sdk_env_base_url_mode(self) -> None:
        settings = _mock_settings(
            resource=None, base_url="https://x.services.ai.azure.com/anthropic", api_key="k2"
        )
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            env = FoundryProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://x.services.ai.azure.com/anthropic",
            "ANTHROPIC_FOUNDRY_API_KEY": "k2",
        }

    def test_claude_sdk_env_resource_wins_over_base_url(self) -> None:
        settings = _mock_settings(
            resource="res-b", base_url="https://ignored.example.com", api_key="k3"
        )
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            env = FoundryProvider().claude_sdk_env()
        assert env["ANTHROPIC_FOUNDRY_RESOURCE"] == "res-b"
        assert "ANTHROPIC_FOUNDRY_BASE_URL" not in env

    def test_claude_model_name_uses_configured_model(self) -> None:
        settings = _mock_settings(model="foundry-custom")
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            assert FoundryProvider().claude_model_name() == "foundry-custom"

    def test_claude_model_name_falls_back_to_default(self) -> None:
        settings = _mock_settings(model=None)
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            assert FoundryProvider().claude_model_name() == "claude-sonnet-4-5"

"""Tests for Azure Foundry provider."""

import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openscientist.providers.foundry import FoundryProvider


def _settings_for_foundry(
    *,
    resource: str | None,
    base_url: str | None,
    api_key: str | None = "foundry-key",
) -> SimpleNamespace:
    provider = SimpleNamespace(
        anthropic_foundry_resource=resource,
        anthropic_foundry_base_url=base_url,
        anthropic_foundry_api_key=api_key,
        anthropic_default_sonnet_model="claude-sonnet-4-5",
        anthropic_default_haiku_model=None,
        anthropic_default_opus_model=None,
    )
    return SimpleNamespace(provider=provider)


@pytest.mark.asyncio
async def test_send_message_derives_foundry_base_url_from_resource():
    expected_url = "https://lab-foundry.services.ai.azure.com/anthropic"
    seen: dict[str, str | None] = {"base_url": None, "api_key": None}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAnthropicClient:
        def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
            seen["base_url"] = base_url
            seen["api_key"] = api_key
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **_kwargs):
            return SimpleNamespace(content=[FakeTextBlock("ok")])

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = FakeAnthropicClient  # type: ignore[attr-defined]

    fake_types = types.ModuleType("anthropic.types")
    fake_types.MessageParam = dict  # type: ignore[attr-defined]
    fake_types.TextBlock = FakeTextBlock  # type: ignore[attr-defined]

    settings = _settings_for_foundry(resource="lab-foundry", base_url=None)
    with (
        patch("openscientist.providers.foundry.get_settings", return_value=settings),
        patch.dict(sys.modules, {"anthropic": fake_anthropic, "anthropic.types": fake_types}),
    ):
        provider = FoundryProvider()
        result = await provider.send_message(messages=[{"role": "user", "content": "hello"}])

    assert result == "ok"
    assert seen["base_url"] == expected_url
    assert seen["api_key"] == "foundry-key"


@pytest.mark.asyncio
async def test_send_message_uses_entra_id_token_when_no_api_key():
    seen: dict[str, str | None] = {"api_key": None}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAnthropicClient:
        def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
            seen["api_key"] = api_key
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **_kwargs):
            return SimpleNamespace(content=[FakeTextBlock("ok")])

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = FakeAnthropicClient  # type: ignore[attr-defined]

    fake_anthropic_types = types.ModuleType("anthropic.types")
    fake_anthropic_types.MessageParam = dict  # type: ignore[attr-defined]
    fake_anthropic_types.TextBlock = FakeTextBlock  # type: ignore[attr-defined]

    # Fake azure.identity with a credential that returns a known token
    fake_token = SimpleNamespace(token="entra-id-token-abc123")
    fake_credential = SimpleNamespace(get_token=lambda _scope: fake_token)
    fake_azure_identity = types.ModuleType("azure.identity")
    fake_azure_identity.DefaultAzureCredential = lambda: fake_credential  # type: ignore[attr-defined]

    settings = _settings_for_foundry(resource="lab-foundry", base_url=None, api_key=None)
    with (
        patch("openscientist.providers.foundry.get_settings", return_value=settings),
        patch.dict(
            sys.modules,
            {
                "anthropic": fake_anthropic,
                "anthropic.types": fake_anthropic_types,
                "azure.identity": fake_azure_identity,
            },
        ),
    ):
        provider = FoundryProvider()
        result = await provider.send_message(messages=[{"role": "user", "content": "hello"}])

    assert result == "ok"
    assert seen["api_key"] == "entra-id-token-abc123"


@pytest.mark.asyncio
async def test_send_message_with_tools_uses_entra_id_token_when_no_api_key():
    seen: dict[str, str | None] = {"api_key": None}

    class FakeToolUseBlock:
        def __init__(self, id: str, name: str, input: dict) -> None:
            self.id = id
            self.name = name
            self.input = input

    class FakeAnthropicClient:
        def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
            seen["api_key"] = api_key
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **_kwargs):
            usage = SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[],
                model="claude-sonnet-4-5",
                usage=usage,
            )

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = FakeAnthropicClient  # type: ignore[attr-defined]

    fake_anthropic_types = types.ModuleType("anthropic.types")
    fake_anthropic_types.ToolParam = dict  # type: ignore[attr-defined]
    fake_anthropic_types.ToolUseBlock = FakeToolUseBlock  # type: ignore[attr-defined]

    fake_token = SimpleNamespace(token="entra-id-token-xyz789")
    fake_credential = SimpleNamespace(get_token=lambda _scope: fake_token)
    fake_azure_identity = types.ModuleType("azure.identity")
    fake_azure_identity.DefaultAzureCredential = lambda: fake_credential  # type: ignore[attr-defined]

    settings = _settings_for_foundry(resource="lab-foundry", base_url=None, api_key=None)
    with (
        patch("openscientist.providers.foundry.get_settings", return_value=settings),
        patch.dict(
            sys.modules,
            {
                "anthropic": fake_anthropic,
                "anthropic.types": fake_anthropic_types,
                "azure.identity": fake_azure_identity,
            },
        ),
    ):
        provider = FoundryProvider()
        result = await provider.send_message_with_tools(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )

    assert seen["api_key"] == "entra-id-token-xyz789"
    assert result["stop_reason"] == "end_turn"


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


class TestFoundrySetupEnvironment:
    """Tests for FoundryProvider.setup_environment() env cleanup."""

    def test_sets_foundry_flag(self) -> None:
        with (
            patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()),
            patch.dict(os.environ, {}, clear=True),
        ):
            FoundryProvider().setup_environment()
            assert os.environ.get("CLAUDE_CODE_USE_FOUNDRY") == "1"

    def test_clears_conflicting_provider_and_auth_vars(self) -> None:
        seeded = {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
            "VERTEX_REGION_CLAUDE_4_5_SONNET": "us-east5",
            "VERTEX_REGION_CLAUDE_4_5_HAIKU": "us-east5",
            "AWS_BEARER_TOKEN_BEDROCK": "bearer-tok",
            "ANTHROPIC_API_KEY": "sk-test",
            "ANTHROPIC_AUTH_TOKEN": "auth-tok",
        }
        with (
            patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()),
            patch.dict(os.environ, seeded, clear=True),
        ):
            FoundryProvider().setup_environment()
            assert os.environ.get("CLAUDE_CODE_USE_FOUNDRY") == "1"
            for var in seeded:
                assert var not in os.environ

    def test_clears_empty_auth_stubs(self) -> None:
        seeded = {
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "ANTHROPIC_BASE_URL": "",
            "AWS_PROFILE": "",
            "AWS_SESSION_TOKEN": "",
        }
        with (
            patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()),
            patch.dict(os.environ, seeded, clear=True),
        ):
            FoundryProvider().setup_environment()
            assert os.environ.get("CLAUDE_CODE_USE_FOUNDRY") == "1"
            for var in seeded:
                assert var not in os.environ


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


class TestFoundryCostAndBaseUrl:
    """Coverage for _resolve_base_url + get_cost_info early paths (Priority-7)."""

    def _provider(self) -> FoundryProvider:
        with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
            return FoundryProvider()

    def test_resolve_base_url_prefers_explicit_url(self) -> None:
        s = _mock_settings(resource="res", base_url="https://explicit.example/anthropic")
        with patch("openscientist.providers.foundry.get_settings", return_value=s):
            assert FoundryProvider()._resolve_base_url() == "https://explicit.example/anthropic"

    def test_resolve_base_url_derives_from_resource(self) -> None:
        s = _mock_settings(resource="lab-foundry", base_url=None)
        with patch("openscientist.providers.foundry.get_settings", return_value=s):
            assert (
                FoundryProvider()._resolve_base_url()
                == "https://lab-foundry.services.ai.azure.com/anthropic"
            )

    def test_resolve_base_url_raises_when_unconfigured(self) -> None:
        provider = self._provider()
        bad = _mock_settings(resource=None, base_url=None, api_key="k")
        with patch("openscientist.providers.foundry.get_settings", return_value=bad):
            with pytest.raises(ValueError, match="endpoint not configured"):
                provider._resolve_base_url()

    def test_get_cost_info_without_subscription_id(self) -> None:
        s = _mock_settings()
        s.provider.azure_subscription_id = None
        with patch("openscientist.providers.foundry.get_settings", return_value=s):
            info = FoundryProvider().get_cost_info()
        assert info.total_spend_usd is None
        assert "AZURE_SUBSCRIPTION_ID" in (info.data_lag_note or "")

    def test_get_cost_info_without_service_principal(self) -> None:
        s = _mock_settings()
        s.provider.azure_subscription_id = "sub-123"
        s.provider.azure_tenant_id = None
        s.provider.azure_client_id = None
        s.provider.azure_client_secret = None
        s.provider.azure_resource_group = None
        with patch("openscientist.providers.foundry.get_settings", return_value=s):
            info = FoundryProvider().get_cost_info()
        assert info.total_spend_usd is None
        assert "AZURE_TENANT_ID" in (info.data_lag_note or "")

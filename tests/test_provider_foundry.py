"""Tests for Azure Foundry provider."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

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
    expected_url = "https://lab-foundry.services.ai.azure.com/api/anthropic"
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
    fake_anthropic.Anthropic = FakeAnthropicClient

    fake_types = types.ModuleType("anthropic.types")
    fake_types.MessageParam = dict
    fake_types.TextBlock = FakeTextBlock

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
    fake_anthropic.Anthropic = FakeAnthropicClient

    fake_anthropic_types = types.ModuleType("anthropic.types")
    fake_anthropic_types.MessageParam = dict
    fake_anthropic_types.TextBlock = FakeTextBlock

    # Fake azure.identity with a credential that returns a known token
    fake_token = SimpleNamespace(token="entra-id-token-abc123")
    fake_credential = SimpleNamespace(get_token=lambda _scope: fake_token)
    fake_azure_identity = types.ModuleType("azure.identity")
    fake_azure_identity.DefaultAzureCredential = lambda: fake_credential  # type: ignore[assignment]

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
    fake_anthropic.Anthropic = FakeAnthropicClient

    fake_anthropic_types = types.ModuleType("anthropic.types")
    fake_anthropic_types.ToolParam = dict
    fake_anthropic_types.ToolUseBlock = FakeToolUseBlock

    fake_token = SimpleNamespace(token="entra-id-token-xyz789")
    fake_credential = SimpleNamespace(get_token=lambda _scope: fake_token)
    fake_azure_identity = types.ModuleType("azure.identity")
    fake_azure_identity.DefaultAzureCredential = lambda: fake_credential  # type: ignore[assignment]

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

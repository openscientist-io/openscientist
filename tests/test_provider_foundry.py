"""Tests for Azure Foundry provider."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shandy.providers.foundry import FoundryProvider


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
        patch("shandy.providers.foundry.get_settings", return_value=settings),
        patch.dict(sys.modules, {"anthropic": fake_anthropic, "anthropic.types": fake_types}),
    ):
        provider = FoundryProvider()
        result = await provider.send_message(messages=[{"role": "user", "content": "hello"}])

    assert result == "ok"
    assert seen["base_url"] == expected_url
    assert seen["api_key"] == "foundry-key"

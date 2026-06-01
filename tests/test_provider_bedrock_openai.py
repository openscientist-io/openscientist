"""Tests for `BedrockOpenAIProvider` (Bedrock Mantle CodexCompatible provider)."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist.providers.base import CodexCompatible
from openscientist.providers.bedrock_openai import BedrockOpenAIProvider


def _settings(
    *,
    region: str = "us-east-1",
    model_default: str = "openai.gpt-oss-120b",
    model: str | None = None,
    stream_max_retries: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        provider=SimpleNamespace(
            bedrock_region=region,
            bedrock_model=model_default,
            bedrock_stream_max_retries=stream_max_retries,
            model=model,
        )
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a key by default so the provider constructs (Provider.__init__
    validates). Individual tests delete it to exercise the error path."""
    monkeypatch.setenv("BEDROCK_API_KEY", "br-key")


def test_is_codex_compatible() -> None:
    with patch("openscientist.providers.bedrock_openai.get_settings", return_value=_settings()):
        assert isinstance(BedrockOpenAIProvider(), CodexCompatible)


def test_identity() -> None:
    with patch("openscientist.providers.bedrock_openai.get_settings", return_value=_settings()):
        p = BedrockOpenAIProvider()
        assert p.id == "bedrock-openai"
        assert p.display_name == "AWS Bedrock OpenAI"
        assert p.codex_model_provider_id() == "bedrock-openai"


def test_config_overrides_use_mantle_responses_surface() -> None:
    with patch(
        "openscientist.providers.bedrock_openai.get_settings",
        return_value=_settings(region="us-west-2"),
    ):
        cfg = tomllib.loads("\n".join(BedrockOpenAIProvider().codex_config_overrides()))
    mp = cfg["model_providers"]["bedrock-openai"]
    assert mp["base_url"] == "https://bedrock-mantle.us-west-2.api.aws/v1"
    assert mp["env_key"] == "BEDROCK_API_KEY"
    assert mp["wire_api"] == "responses"
    assert mp["stream_max_retries"] == 5


def test_model_name_defaults_to_gpt_oss() -> None:
    with patch(
        "openscientist.providers.bedrock_openai.get_settings",
        return_value=_settings(model=None),
    ):
        assert BedrockOpenAIProvider().codex_model_name() == "openai.gpt-oss-120b"


def test_model_override_wins() -> None:
    with patch(
        "openscientist.providers.bedrock_openai.get_settings",
        return_value=_settings(model="openai.gpt-oss-20b"),
    ):
        assert BedrockOpenAIProvider().codex_model_name() == "openai.gpt-oss-20b"


def test_codex_sdk_env_carries_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("openscientist.providers.bedrock_openai.get_settings", return_value=_settings()):
        p = BedrockOpenAIProvider()
    assert p.codex_sdk_env() == {"BEDROCK_API_KEY": "br-key"}
    monkeypatch.delenv("BEDROCK_API_KEY")
    assert p.codex_sdk_env() == {}


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    with patch("openscientist.providers.bedrock_openai.get_settings", return_value=_settings()):
        with pytest.raises(ValueError, match="BEDROCK_API_KEY"):
            BedrockOpenAIProvider()


def test_get_cost_info_unavailable() -> None:
    with patch("openscientist.providers.bedrock_openai.get_settings", return_value=_settings()):
        info = BedrockOpenAIProvider().get_cost_info()
    assert info.total_spend_usd is None
    assert info.recent_spend_usd is None


def test_get_provider_selects_bedrock_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """`provider_id="bedrock-openai"` resolves to BedrockOpenAIProvider via the factory."""
    from openscientist.providers import get_provider
    from openscientist.settings import clear_settings_cache

    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "bedrock-openai")
    clear_settings_cache()
    try:
        assert isinstance(get_provider(), BedrockOpenAIProvider)
    finally:
        clear_settings_cache()

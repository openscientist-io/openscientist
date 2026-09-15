"""Tests for `AzureOpenAIProvider` (Azure-hosted CodexCompatible provider)."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist.providers.azure_openai import AzureOpenAIProvider
from openscientist.providers.base import CodexCompatible


def _settings(
    *,
    resource: str | None = "myres",
    api_version: str | None = None,
    model: str | None = "mydep",
    stream_max_retries: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        provider=SimpleNamespace(
            azure_openai_resource=resource,
            azure_openai_api_version=api_version,
            azure_openai_stream_max_retries=stream_max_retries,
            model=model,
        )
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a key by default so the provider constructs (Provider.__init__
    validates). Individual tests delete it to exercise the error path."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")


def test_is_codex_compatible() -> None:
    with patch("openscientist.providers.azure_openai.get_settings", return_value=_settings()):
        assert isinstance(AzureOpenAIProvider(), CodexCompatible)


def test_identity() -> None:
    with patch("openscientist.providers.azure_openai.get_settings", return_value=_settings()):
        p = AzureOpenAIProvider()
        assert p.id == "azure-openai"
        assert p.display_name == "Azure OpenAI Service"
        assert p.codex_model_provider_id() == "azure-openai"


def test_config_overrides_use_v1_responses_surface() -> None:
    # Azure serves Responses at .../openai/v1/responses (codex appends
    # "/responses"), not under /deployments/<name>/.
    with patch(
        "openscientist.providers.azure_openai.get_settings",
        return_value=_settings(resource="myres", api_version=None),
    ):
        cfg = tomllib.loads("\n".join(AzureOpenAIProvider().codex_config_overrides()))
    mp = cfg["model_providers"]["azure-openai"]
    assert mp["base_url"] == "https://myres.openai.azure.com/openai/v1"
    assert mp["env_key"] == "AZURE_OPENAI_API_KEY"
    assert mp["wire_api"] == "responses"
    # Resilience against Azure's intermittent streaming disconnects.
    assert mp["stream_max_retries"] == 10
    # api-version is omitted on the v1 surface unless explicitly configured.
    assert "query_params" not in mp


def test_api_version_pinned_only_when_configured() -> None:
    with patch(
        "openscientist.providers.azure_openai.get_settings",
        return_value=_settings(api_version="2025-04-01-preview"),
    ):
        cfg = tomllib.loads("\n".join(AzureOpenAIProvider().codex_config_overrides()))
    assert (
        cfg["model_providers"]["azure-openai"]["query_params"]["api-version"]
        == "2025-04-01-preview"
    )


def test_model_name_is_the_configured_model() -> None:
    with patch(
        "openscientist.providers.azure_openai.get_settings",
        return_value=_settings(model="gpt-5"),
    ):
        assert AzureOpenAIProvider().codex_model_name() == "gpt-5"


def test_codex_sdk_env_carries_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("openscientist.providers.azure_openai.get_settings", return_value=_settings()):
        p = AzureOpenAIProvider()
    assert p.codex_sdk_env() == {"AZURE_OPENAI_API_KEY": "az-key"}
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    assert p.codex_sdk_env() == {}


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    with patch("openscientist.providers.azure_openai.get_settings", return_value=_settings()):
        with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
            AzureOpenAIProvider()


def test_missing_resource_raises() -> None:
    with patch(
        "openscientist.providers.azure_openai.get_settings",
        return_value=_settings(resource=None),
    ):
        with pytest.raises(ValueError, match="AZURE_OPENAI_RESOURCE"):
            AzureOpenAIProvider()


def test_missing_model_raises() -> None:
    with patch(
        "openscientist.providers.azure_openai.get_settings",
        return_value=_settings(model=None),
    ):
        with pytest.raises(ValueError, match="OPENSCIENTIST_MODEL"):
            AzureOpenAIProvider()


def test_get_cost_info_unavailable() -> None:
    with patch("openscientist.providers.azure_openai.get_settings", return_value=_settings()):
        info = AzureOpenAIProvider().get_cost_info()
    assert info.total_spend_usd is None
    assert info.recent_spend_usd is None


def test_get_provider_selects_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`provider_id="azure-openai"` resolves to AzureOpenAIProvider via the factory."""
    from openscientist.providers import get_provider
    from openscientist.settings import clear_settings_cache

    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "azure-openai")
    monkeypatch.setenv("AZURE_OPENAI_RESOURCE", "myres")
    monkeypatch.setenv("OPENSCIENTIST_MODEL", "mydep")
    clear_settings_cache()
    try:
        assert isinstance(get_provider(), AzureOpenAIProvider)
    finally:
        clear_settings_cache()

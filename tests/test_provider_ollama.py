"""Tests for `OllamaProvider` (local, keyless CodexCompatible provider)."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist.providers.base import CodexCompatible
from openscientist.providers.ollama import OllamaProvider


def _settings(
    *,
    base_url: str = "http://localhost:11434/v1",
    model_default: str = "gpt-oss:20b",
    model: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        provider=SimpleNamespace(
            ollama_base_url=base_url,
            ollama_model=model_default,
            model=model,
        )
    )


def test_is_codex_compatible() -> None:
    with patch("openscientist.providers.ollama.get_settings", return_value=_settings()):
        assert isinstance(OllamaProvider(), CodexCompatible)


def test_identity() -> None:
    with patch("openscientist.providers.ollama.get_settings", return_value=_settings()):
        p = OllamaProvider()
        assert p.id == "ollama"
        assert p.display_name == "Ollama (local)"
        # codex reserves "ollama" as a built-in id, so the config table and
        # model_provider use "ollama-local".
        assert p.codex_model_provider_id() == "ollama-local"


def test_config_overrides_are_keyless_responses_surface() -> None:
    with patch(
        "openscientist.providers.ollama.get_settings",
        return_value=_settings(base_url="http://host.docker.internal:11434/v1"),
    ):
        cfg = tomllib.loads("\n".join(OllamaProvider().codex_config_overrides()))
    mp = cfg["model_providers"]["ollama-local"]
    assert mp["base_url"] == "http://host.docker.internal:11434/v1"
    assert mp["wire_api"] == "responses"
    # Local and keyless: codex must not demand an OpenAI login, and no env_key
    # is sent.
    assert mp["requires_openai_auth"] is False
    assert "env_key" not in mp
    # Slow CPU-offloaded models (gpt-oss:120b) prefill in silence for minutes;
    # a long stream idle timeout keeps codex from dropping the SSE connection.
    assert mp["stream_idle_timeout_ms"] == 3600000


def test_model_name_defaults_to_ollama_model() -> None:
    with patch(
        "openscientist.providers.ollama.get_settings",
        return_value=_settings(model=None),
    ):
        assert OllamaProvider().codex_model_name() == "gpt-oss:20b"


def test_model_override_wins() -> None:
    with patch(
        "openscientist.providers.ollama.get_settings",
        return_value=_settings(model="qwen2.5-coder:32b"),
    ):
        assert OllamaProvider().codex_model_name() == "qwen2.5-coder:32b"


def test_codex_sdk_env_is_empty() -> None:
    with patch("openscientist.providers.ollama.get_settings", return_value=_settings()):
        assert OllamaProvider().codex_sdk_env() == {}


def test_validate_required_config_is_empty() -> None:
    """Local and keyless: nothing the operator must supply for construction."""
    with patch("openscientist.providers.ollama.get_settings", return_value=_settings()):
        assert OllamaProvider().validate_required_config() == []


def test_get_cost_info_reports_zero_local_spend() -> None:
    with patch("openscientist.providers.ollama.get_settings", return_value=_settings()):
        info = OllamaProvider().get_cost_info()
    assert info.total_spend_usd == 0.0
    assert info.recent_spend_usd == 0.0


def test_get_provider_selects_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """`provider_id="ollama"` resolves to OllamaProvider via the factory."""
    from openscientist.providers import get_provider
    from openscientist.settings import clear_settings_cache

    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "ollama")
    clear_settings_cache()
    try:
        assert isinstance(get_provider(), OllamaProvider)
    finally:
        clear_settings_cache()

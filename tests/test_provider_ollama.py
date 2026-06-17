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
    model_context_tokens: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        provider=SimpleNamespace(
            ollama_base_url=base_url,
            ollama_model=model_default,
            model=model,
            model_context_tokens=model_context_tokens,
        )
    )


def _resp(payload: dict) -> object:
    """A fake requests.Response returning ``payload`` from .json()."""
    from unittest.mock import MagicMock

    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


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


# --- context window probe (_ollama_http_base / _probe_ollama_context_tokens) ----


def test_ollama_http_base_strips_v1() -> None:
    from openscientist.providers.ollama import _ollama_http_base

    assert _ollama_http_base("http://host:11434/v1") == "http://host:11434"
    assert _ollama_http_base("http://host:11434/v1/") == "http://host:11434"
    assert _ollama_http_base("http://host:11434") == "http://host:11434"


def test_probe_reads_loaded_context_from_api_ps() -> None:
    from openscientist.providers import ollama as ollama_mod
    from openscientist.providers.ollama import _probe_ollama_context_tokens

    payload = {"models": [{"name": "gpt-oss:120b", "context_length": 131072}]}
    with patch.object(ollama_mod.requests, "get", return_value=_resp(payload)) as get:
        assert _probe_ollama_context_tokens("http://h:11434/v1", "gpt-oss:120b") == 131072
    get.assert_called_once_with("http://h:11434/api/ps", timeout=5)


def test_probe_matches_model_name_prefix() -> None:
    from openscientist.providers import ollama as ollama_mod
    from openscientist.providers.ollama import _probe_ollama_context_tokens

    payload = {"models": [{"name": "gpt-oss:120b-q4", "context_length": 40000}]}
    with patch.object(ollama_mod.requests, "get", return_value=_resp(payload)):
        assert _probe_ollama_context_tokens("http://h:11434/v1", "gpt-oss:120b") == 40000


def test_probe_falls_back_to_api_show_when_not_loaded() -> None:
    from openscientist.providers import ollama as ollama_mod
    from openscientist.providers.ollama import _probe_ollama_context_tokens

    with (
        patch.object(ollama_mod.requests, "get", return_value=_resp({"models": []})),
        patch.object(
            ollama_mod.requests,
            "post",
            return_value=_resp({"model_info": {"gptoss.context_length": 131072}}),
        ) as post,
    ):
        assert _probe_ollama_context_tokens("http://h:11434/v1", "gpt-oss:120b") == 131072
    post.assert_called_once()


def test_probe_returns_none_on_empty_responses() -> None:
    from openscientist.providers import ollama as ollama_mod
    from openscientist.providers.ollama import _probe_ollama_context_tokens

    with (
        patch.object(ollama_mod.requests, "get", return_value=_resp({"models": []})),
        patch.object(ollama_mod.requests, "post", return_value=_resp({"model_info": {}})),
    ):
        assert _probe_ollama_context_tokens("http://h:11434/v1", "gpt-oss:120b") is None


def test_probe_returns_none_on_connection_error() -> None:
    import requests

    from openscientist.providers import ollama as ollama_mod
    from openscientist.providers.ollama import _probe_ollama_context_tokens

    with (
        patch.object(ollama_mod.requests, "get", side_effect=requests.ConnectionError("down")),
        patch.object(ollama_mod.requests, "post", side_effect=requests.ConnectionError("down")),
    ):
        assert _probe_ollama_context_tokens("http://h:11434/v1", "gpt-oss:120b") is None


# --- OllamaProvider.model_profile (override / live probe / failure) -------------


def test_model_profile_override_wins_without_probing() -> None:
    with (
        patch(
            "openscientist.providers.ollama.get_settings",
            return_value=_settings(model_context_tokens=65536),
        ),
        patch("openscientist.providers.ollama._probe_ollama_context_tokens") as probe,
    ):
        profile = OllamaProvider().model_profile()
    assert profile.context_window_tokens == 65536
    probe.assert_not_called()


def test_model_profile_uses_live_probe() -> None:
    with (
        patch("openscientist.providers.ollama.get_settings", return_value=_settings()),
        patch(
            "openscientist.providers.ollama._probe_ollama_context_tokens", return_value=131072
        ) as probe,
    ):
        profile = OllamaProvider().model_profile()
    assert profile.id == "gpt-oss:20b"
    assert profile.context_window_tokens == 131072
    probe.assert_called_once_with("http://localhost:11434/v1", "gpt-oss:20b")


def test_model_profile_probe_failure_logs_warning_and_defaults(caplog) -> None:
    import logging

    from openscientist import models

    with (
        patch("openscientist.providers.ollama.get_settings", return_value=_settings()),
        patch("openscientist.providers.ollama._probe_ollama_context_tokens", return_value=None),
        caplog.at_level(logging.WARNING, logger="openscientist.providers.ollama"),
    ):
        profile = OllamaProvider().model_profile()
    assert profile.context_window_tokens == models._DEFAULT_CONTEXT_TOKENS
    assert any("Could not probe" in r.message for r in caplog.records)

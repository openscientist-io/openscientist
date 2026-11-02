"""Tests for the ModelProfile value object and hosted-model resolution.

Provider-specific resolution (the Ollama probe and ``OllamaProvider.model_profile``)
lives in ``tests/test_provider_ollama.py``; this file covers the value object, the
known-model table, and the shared ``default_model_profile`` used by hosted providers.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import requests

from openscientist import models
from openscientist.models import (
    ModelProfile,
    _known_context_tokens,
    default_model_profile,
)
from openscientist.providers import pricing
from openscientist.providers.base import CostInfo, Provider


def test_profile_is_frozen():
    p = ModelProfile(id="m", context_window_tokens=1000)
    try:
        p.context_window_tokens = 5  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ModelProfile should be frozen")


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-opus-5", 1_000_000),
        ("claude-sonnet-5-5", 1_000_000),
        ("us.anthropic.claude-opus-4-6-v1", 1_000_000),
        ("claude-opus-4-6@20260101", 1_000_000),
        ("claude-sonnet-4-6", 1_000_000),
        ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0", 200_000),
        ("global.anthropic.claude-haiku-5-5", 1_000_000),
        ("claude-opus-4-5@20251101", 200_000),
    ],
)
def test_hosted_claude_ids_resolve_offline(model_id: str, expected: int):
    """Anthropic, Bedrock and Vertex spellings of current Claude models resolve
    from the offline table when the litellm database is unavailable."""
    assert default_model_profile(model_id, override=None).context_window_tokens == expected


def test_known_context_tokens_picks_longest_prefix():
    # Two entries match the same id; the longer (more specific) prefix wins.
    with patch.dict(models._KNOWN_CONTEXT_TOKENS, {"gpt-4": 8_000, "gpt-4o": 128_000}, clear=False):
        assert _known_context_tokens("gpt-4o") == 128_000
        assert _known_context_tokens("gpt-4-turbo") == 8_000


def test_known_context_tokens_misses_an_unknown_model():
    assert _known_context_tokens("totally-unknown-model") is None


def test_catalog_window_wins_over_the_offline_table():
    catalog = {"claude-opus-5": {"max_input_tokens": 500_000}}
    profile = default_model_profile("claude-opus-5", override=None, catalog=catalog)
    assert profile.context_window_tokens == 500_000


def test_catalog_exact_id_is_tried_before_the_normalised_one():
    catalog = {
        "us.anthropic.claude-new-9-v1:0": {"max_input_tokens": 300_000},
        "claude-new-9": {"max_input_tokens": 100_000},
    }
    profile = default_model_profile("us.anthropic.claude-new-9-v1:0", None, catalog=catalog)
    assert profile.context_window_tokens == 300_000


def test_catalog_normalised_id_resolves_a_prefixed_model():
    catalog = {"claude-new-9": {"max_input_tokens": 100_000}}
    assert (
        default_model_profile(
            "eu.anthropic.claude-new-9-v1:0", None, catalog=catalog
        ).context_window_tokens
        == 100_000
    )
    assert (
        default_model_profile("claude-new-9@20270101", None, catalog=catalog).context_window_tokens
        == 100_000
    )


def test_catalog_entry_without_a_window_falls_back_to_the_table():
    """The offline pricing fallback carries rates only, so it must not mask the table."""
    profile = default_model_profile(
        "claude-sonnet-5-5", override=None, catalog=pricing._FALLBACK_PRICING
    )
    assert profile.context_window_tokens == 1_000_000


def test_override_wins_over_the_catalog_and_the_table():
    catalog = {"claude-opus-5": {"max_input_tokens": 500_000}}
    profile = default_model_profile("claude-opus-5", override=65536, catalog=catalog)
    assert profile.id == "claude-opus-5"
    assert profile.context_window_tokens == 65536


def test_falling_back_to_the_default_warns_with_the_fix(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="openscientist.models"):
        profile = default_model_profile("mystery-model", override=None, catalog={})
    assert profile.context_window_tokens == models._DEFAULT_CONTEXT_TOKENS
    assert "OPENSCIENTIST_MODEL_CONTEXT_TOKENS" in caplog.text


def test_a_resolved_window_does_not_warn(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="openscientist.models"):
        default_model_profile("claude-opus-5", override=None)
    assert not caplog.records


def test_default_model_profile_handles_missing_model_id():
    profile = default_model_profile(None, override=None)
    assert profile.id == "unknown"
    assert profile.context_window_tokens == models._DEFAULT_CONTEXT_TOKENS


class _FakeProvider(Provider):
    @property
    def id(self) -> str:
        return "fake"

    display_name = "Fake"

    def validate_required_config(self) -> list[str]:
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        return CostInfo(
            provider_name="Fake",
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
        )

    def effective_model_name(self) -> str | None:
        return "claude-new-9"

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        return {}


def _settings(context_tokens: int | None) -> SimpleNamespace:
    return SimpleNamespace(provider=SimpleNamespace(model_context_tokens=context_tokens))


def test_provider_profile_reads_the_window_from_the_litellm_database():
    catalog: dict[str, Any] = {"claude-new-9": {"max_input_tokens": 750_000}}
    with (
        patch("openscientist.providers.base.get_settings", return_value=_settings(None)),
        patch("openscientist.providers.pricing._get_litellm_pricing", return_value=catalog),
    ):
        profile = _FakeProvider().model_profile()
    assert profile.id == "claude-new-9"
    assert profile.context_window_tokens == 750_000


def test_provider_profile_uses_the_table_when_the_database_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
):
    """Offline, the pricing fetch falls back to its rate-only table, and the window
    still comes from the static table rather than the 8192 default."""

    def unreachable(*args: object, **kwargs: object) -> None:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(pricing, "_cache", {})
    monkeypatch.setattr(pricing, "_cache_fetched_at", 0.0)
    monkeypatch.setattr(pricing.requests, "get", unreachable)
    monkeypatch.setattr(_FakeProvider, "effective_model_name", lambda self: "claude-opus-5")
    with patch("openscientist.providers.base.get_settings", return_value=_settings(None)):
        profile = _FakeProvider().model_profile()
    assert profile.context_window_tokens == 1_000_000


def test_provider_profile_with_an_override_never_fetches_the_database():
    def must_not_fetch() -> dict[str, Any]:
        raise AssertionError("the litellm database was fetched although the window is pinned")

    with (
        patch("openscientist.providers.base.get_settings", return_value=_settings(4096)),
        patch("openscientist.providers.pricing._get_litellm_pricing", side_effect=must_not_fetch),
    ):
        assert _FakeProvider().model_profile().context_window_tokens == 4096


@pytest.mark.network
def test_offline_table_matches_the_live_litellm_windows():
    """The offline table is hand-kept, so check each entry against litellm, where
    a drift fails in front of somebody who can fix it."""
    resp = requests.get(pricing._LITELLM_PRICING_URL, timeout=30)
    resp.raise_for_status()
    live = resp.json()
    for prefix, tokens in models._KNOWN_CONTEXT_TOKENS.items():
        entry = live.get(prefix)
        if isinstance(entry, dict) and "max_input_tokens" in entry:
            assert entry["max_input_tokens"] == tokens, f"{prefix} drifted"

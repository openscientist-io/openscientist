"""Tests for the ModelProfile value object and hosted-model resolution.

Provider-specific resolution (the Ollama probe and ``OllamaProvider.model_profile``)
lives in ``tests/test_provider_ollama.py``; this file covers the value object, the
known-model table, and the shared ``default_model_profile`` used by hosted providers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from openscientist import models
from openscientist.models import (
    ModelProfile,
    _known_context_tokens,
    default_model_profile,
)


def test_profile_is_frozen():
    p = ModelProfile(id="m", context_window_tokens=1000)
    try:
        p.context_window_tokens = 2  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ModelProfile should be frozen")


def test_known_context_tokens_prefix_match():
    assert _known_context_tokens("claude-sonnet-4-6") == 200_000
    assert _known_context_tokens("gpt-4o-mini") == 128_000
    assert _known_context_tokens("totally-unknown-model") is None


def test_known_context_tokens_picks_longest_prefix():
    # Two entries match the same id; the longer (more specific) prefix wins.
    with patch.dict(models._KNOWN_CONTEXT_TOKENS, {"gpt-4": 8_000, "gpt-4o": 128_000}, clear=False):
        assert _known_context_tokens("gpt-4o") == 128_000
        assert _known_context_tokens("gpt-4-turbo") == 8_000


def test_default_model_profile_override_wins():
    profile = default_model_profile("claude-sonnet-4-6", override=65536)
    assert profile.id == "claude-sonnet-4-6"
    assert profile.context_window_tokens == 65536


def test_default_model_profile_uses_known_table():
    assert (
        default_model_profile("claude-sonnet-4-6", override=None).context_window_tokens == 200_000
    )


def test_default_model_profile_falls_back_to_default():
    profile = default_model_profile("mystery-model", override=None)
    assert profile.context_window_tokens == models._DEFAULT_CONTEXT_TOKENS


def test_default_model_profile_handles_missing_model_id():
    profile = default_model_profile(None, override=None)
    assert profile.id == "unknown"
    assert profile.context_window_tokens == models._DEFAULT_CONTEXT_TOKENS


def test_base_provider_model_profile_delegates_to_default():
    """The base Provider.model_profile resolves via default_model_profile:
    explicit override, then known-model table, using effective_model_name()."""
    from openscientist.providers.base import CostInfo, Provider

    class _FakeProvider(Provider):
        @property
        def id(self) -> str:
            return "fake"

        @property
        def display_name(self) -> str:
            return "Fake"

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
            return "claude-sonnet-4-6"

    provider = _FakeProvider()
    with patch(
        "openscientist.providers.base.get_settings",
        return_value=SimpleNamespace(provider=SimpleNamespace(model_context_tokens=None)),
    ):
        profile = provider.model_profile()
    assert profile.id == "claude-sonnet-4-6"
    assert profile.context_window_tokens == 200_000

    with patch(
        "openscientist.providers.base.get_settings",
        return_value=SimpleNamespace(provider=SimpleNamespace(model_context_tokens=4096)),
    ):
        assert provider.model_profile().context_window_tokens == 4096

"""Tests for `openscientist.providers.pricing`.

Most of these patch the table rather than asserting real rates, which would break
whenever litellm updates. The one exception is the parity check on the offline
fallback table, which has to compare against live data and is marked `network`.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import patch

import pytest
import requests

from openscientist.models import normalize_model_name
from openscientist.providers import provider_class, provider_ids
from openscientist.providers.base import ClaudeCompatible
from openscientist.providers.pricing import (
    _FALLBACK_PRICING,
    _LITELLM_PRICING_URL,
    estimate_cost_usd,
)
from openscientist.settings import get_settings

_ENTRY = {
    "input_cost_per_token": 5e-6,
    "output_cost_per_token": 25e-6,
    "cache_read_input_token_cost": 0.5e-6,
    "cache_creation_input_token_cost": 6.25e-6,
    "cache_creation_input_token_cost_above_1hr": 10e-6,
}


def _patched(entry: dict[str, float] | None = None) -> AbstractContextManager[Any]:
    return patch(
        "openscientist.providers.pricing._get_litellm_pricing",
        return_value={"m": entry if entry is not None else _ENTRY},
    )


def test_every_billed_bucket_is_priced() -> None:
    """Cached reads dominate agentic runs; omitting them understates badly."""
    with _patched():
        cost = estimate_cost_usd(
            "m",
            1_000_000,
            1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
            cache_write_1h_tokens=1_000_000,
            reasoning_tokens=1_000_000,
        )
    # input 5 + output 25 + read 0.5 + write 6.25 + write-1h 10 + reasoning at output 25
    assert cost == 71.75


def test_reasoning_tokens_bill_as_output() -> None:
    with _patched():
        assert estimate_cost_usd("m", 0, 0, reasoning_tokens=1_000_000) == 25.0


def test_cache_buckets_default_to_zero_so_old_callers_are_unchanged() -> None:
    with _patched():
        assert estimate_cost_usd("m", 1_000_000, 1_000_000) == 30.0


def test_cache_read_rate_defaults_to_a_tenth_of_the_input_rate() -> None:
    """Most priced litellm entries carry no cache-read rate, so this is the common
    path. Charging the full input rate there overstates a cached read tenfold."""
    with _patched({"input_cost_per_token": 2e-6, "output_cost_per_token": 8e-6}):
        assert estimate_cost_usd("m", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.2)


def test_cache_write_rate_defaults_to_a_quarter_above_the_input_rate() -> None:
    with _patched({"input_cost_per_token": 2e-6, "output_cost_per_token": 8e-6}):
        assert estimate_cost_usd("m", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(2.5)


def test_one_hour_cache_writes_bill_at_twice_the_input_rate() -> None:
    """A one-hour cache entry costs 2x base input against 1.25x for five minutes, and
    litellm publishes the two separately, so the tiers cannot share a rate."""
    with _patched():
        assert estimate_cost_usd("m", 0, 0, cache_write_1h_tokens=1_000_000) == pytest.approx(10.0)


def test_one_hour_write_rate_defaults_to_twice_the_input_rate() -> None:
    """Only 126 litellm entries carry the one-hour rate, so the ratio is the usual path."""
    with _patched({"input_cost_per_token": 2e-6, "output_cost_per_token": 8e-6}):
        assert estimate_cost_usd("m", 0, 0, cache_write_1h_tokens=1_000_000) == pytest.approx(4.0)


def test_cache_read_rate_uses_the_second_published_key_when_the_first_is_absent() -> None:
    """The DeepSeek entries publish their read rate only as
    `input_cost_per_token_cache_hit`, so looking at one key sent them to the 0.1x
    ratio and undercharged the r1 variants, whose real ratio is 0.255."""
    entry = {
        "input_cost_per_token": 2e-6,
        "output_cost_per_token": 8e-6,
        "input_cost_per_token_cache_hit": 0.51e-6,
    }
    with _patched(entry):
        assert estimate_cost_usd("m", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.51)


def test_a_published_zero_cache_rate_beats_the_derived_ratio() -> None:
    """litellm prices cache reads free on some entries. Treating 0.0 as missing would
    silently charge a tenth of the input rate for something the provider gives away."""
    entry = {
        "input_cost_per_token": 2e-6,
        "output_cost_per_token": 8e-6,
        "cache_read_input_token_cost": 0.0,
        "cache_creation_input_token_cost": 0.0,
    }
    with _patched(entry):
        assert (
            estimate_cost_usd("m", 0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
            == 0.0
        )


def test_fallback_pricing_carries_its_own_cache_rates() -> None:
    """The offline table must not leave cache reads to the input rate. On the shape
    from the PR description (500k in, 100k out, 2M cache reads, 200k cache writes) it
    charged $48.00, and at Opus 4.6's real rates it is $7.25."""
    with patch(
        "openscientist.providers.pricing._get_litellm_pricing",
        return_value=_FALLBACK_PRICING,
    ):
        cost = estimate_cost_usd(
            "claude-opus-4-6",
            500_000,
            100_000,
            cache_read_tokens=2_000_000,
            cache_write_tokens=200_000,
        )
    assert cost == pytest.approx(7.25)


@pytest.mark.parametrize(
    "provider_id",
    [p for p in provider_ids() if issubclass(provider_class(p), ClaudeCompatible)],
)
def test_every_provider_default_model_has_an_offline_price(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default that falls out of the table is priced at $0 whenever litellm cannot
    be reached, and a model missing from litellm is usually a retired one. Sonnet 4
    stayed the Anthropic default for months after its retirement."""
    monkeypatch.delenv("OPENSCIENTIST_MODEL", raising=False)
    get_settings.cache_clear()
    cls = provider_class(provider_id)
    # Built without __init__, which validates credentials this test does not need.
    default = cls.__new__(cls).claude_model_name()  # type: ignore[attr-defined]
    assert normalize_model_name(default) in _FALLBACK_PRICING, default
    get_settings.cache_clear()


@pytest.mark.network
def test_fallback_pricing_has_not_drifted_from_the_live_table() -> None:
    """The offline table is hand-maintained and drifted unnoticed: Opus 4.6 sat at
    $15/M input long after litellm moved it to $5/M, and the cache rates derived from
    it inherited the error. Marked `network` because litellm repricing should fail here,
    where somebody can act on it, rather than in CI on an unrelated merge."""
    resp = requests.get(_LITELLM_PRICING_URL, timeout=30)
    resp.raise_for_status()
    live = resp.json()

    for model, entry in _FALLBACK_PRICING.items():
        assert model in live, f"{model} is no longer a litellm key"
        for key, rate in entry.items():
            assert live[model].get(key) == pytest.approx(rate), f"{model}.{key} drifted"


def test_unknown_model_warns_rather_than_silently_returning_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A silent 0.0 is indistinguishable from a genuinely free run, which is how
    prod recorded $0 for every Opus job on an unrecognised deployment name."""
    with _patched(), caplog.at_level(logging.WARNING):
        assert estimate_cost_usd("not-a-model", 1_000_000, 1_000_000) == 0.0
    assert "No pricing entry" in caplog.text


def test_bedrock_and_vertex_suffixes_normalise_to_litellm_keys() -> None:
    assert normalize_model_name("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == (
        "claude-sonnet-4-5"
    )
    assert normalize_model_name("claude-sonnet-4-5@20250929") == "claude-sonnet-4-5"
    assert normalize_model_name("us.anthropic.claude-opus-4-6-v1") == "claude-opus-4-6"
    assert normalize_model_name("global.anthropic.claude-haiku-5-5-v1:0") == "claude-haiku-5-5"
    assert normalize_model_name("us-gov.anthropic.claude-opus-5") == "claude-opus-5"
    assert normalize_model_name("claude-opus-5@default") == "claude-opus-5"


def test_a_version_ending_outside_claude_stays_part_of_the_name() -> None:
    assert normalize_model_name("deepseek-v3") == "deepseek-v3"

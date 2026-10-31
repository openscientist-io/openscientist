"""LLM model pricing lookup via the litellm pricing database."""

import logging
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
_CACHE_TTL_SECONDS = 86_400

# Anthropic's published cache ratios (checked across Opus, Sonnet and Haiku).
# 0.1 is also the modal read/input ratio among litellm entries that carry one.
# A cache write costs more the longer it lives, which is why the two write tiers
# differ.
_CACHE_READ_RATE_RATIO = 0.1
_CACHE_WRITE_RATE_RATIO = 1.25
_CACHE_WRITE_1H_RATE_RATIO = 2.0

# Rate keys in priority order. litellm publishes the DeepSeek family's cache-read
# rate under the second name only, so reading just the first would fall back to
# the ratio even though an exact rate is present.
_CACHE_READ_KEYS = ("cache_read_input_token_cost", "input_cost_per_token_cache_hit")
_CACHE_WRITE_KEYS = ("cache_creation_input_token_cost",)
_CACHE_WRITE_1H_KEYS = ("cache_creation_input_token_cost_above_1hr",)

_cache: dict[str, Any] = {}
_cache_fetched_at: float = 0.0


def _get_litellm_pricing() -> dict[str, Any]:
    """Fetch (and cache for 24h) the litellm model pricing database."""
    global _cache, _cache_fetched_at
    if _cache and (time.monotonic() - _cache_fetched_at) < _CACHE_TTL_SECONDS:
        return _cache
    try:
        resp = requests.get(_LITELLM_PRICING_URL, timeout=10)
        resp.raise_for_status()
        _cache = resp.json()
        _cache_fetched_at = time.monotonic()
        logger.debug("Fetched litellm pricing database (%d entries)", len(_cache))
    except Exception as e:
        logger.warning("Failed to fetch litellm pricing database: %s", e)
        if not _cache:
            _cache = _FALLBACK_PRICING
    return _cache


def normalize_model_name(model: str) -> str:
    """Strip provider-specific prefixes and suffixes to get a litellm-compatible key."""
    model = re.sub(r"^(?:us|eu|ap)\.anthropic\.", "", model)
    model = re.sub(r"-\d{8}.*$", "", model)
    model = re.sub(r"@\d{8}$", "", model)
    return model


def _published_rate(entry: dict[str, Any], keys: tuple[str, ...], derived: float) -> float:
    """First rate published under ``keys``, else ``derived``.

    Tests presence rather than truthiness: a published 0.0 is a real rate (litellm
    prices cache reads free on 15 entries and cache writes free on 29), so it has to
    win over the derived ratio instead of being treated as missing.
    """
    for key in keys:
        rate = entry.get(key)
        if isinstance(rate, (int, float)):
            return float(rate)
    return derived


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    """Estimate cost in USD using the litellm pricing database.

    Prices every billed bucket. Cached reads dominate agentic runs, so omitting
    them understates cost while the total stays plausible. Reasoning tokens are
    billed as output where a provider reports them separately.

    Cache rates come from the first key the entry publishes them under, else the
    input rate times the ratios above. Charging the full input rate for a cached
    read overstates it roughly tenfold. ``cache_write_tokens`` is the short-lived
    tier and ``cache_write_1h_tokens`` the one-hour tier at twice the input rate.

    The buckets are keyword-only. They are easy to transpose and each is priced
    differently, so a positional call would misprice silently. A missing entry
    returns 0.0 with a warning, so the zero stays visible in logs.
    """
    pricing = _get_litellm_pricing()
    entry = pricing.get(model) or pricing.get(normalize_model_name(model))
    if not entry:
        logger.warning(
            "No pricing entry for model %r (normalised %r); reporting $0.00. "
            "Cost tracking for this model is not meaningful until a rate is added.",
            model,
            normalize_model_name(model),
        )
        return 0.0
    in_rate = float(entry.get("input_cost_per_token", 0.0))
    out_rate = float(entry.get("output_cost_per_token", 0.0))
    read_rate = _published_rate(entry, _CACHE_READ_KEYS, in_rate * _CACHE_READ_RATE_RATIO)
    write_rate = _published_rate(entry, _CACHE_WRITE_KEYS, in_rate * _CACHE_WRITE_RATE_RATIO)
    write_1h_rate = _published_rate(
        entry, _CACHE_WRITE_1H_KEYS, in_rate * _CACHE_WRITE_1H_RATE_RATIO
    )
    return (
        in_rate * input_tokens
        + out_rate * output_tokens
        + read_rate * cache_read_tokens
        + write_rate * cache_write_tokens
        + write_1h_rate * cache_write_1h_tokens
        + out_rate * reasoning_tokens
    )


# Fallback used only when the remote fetch fails and the cache is empty. Every rate
# matches the live litellm table, which the `network`-marked parity test checks, since
# a hand-maintained table drifts silently otherwise.
_FALLBACK_PRICING: dict[str, Any] = {
    "claude-opus-4-6": {
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 25e-6,
        "cache_read_input_token_cost": 0.5e-6,
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_creation_input_token_cost_above_1hr": 10e-6,
    },
    "claude-sonnet-5-5": {
        "input_cost_per_token": 2e-6,
        "output_cost_per_token": 10e-6,
        "cache_read_input_token_cost": 0.1e-6,
        "cache_creation_input_token_cost": 2.5e-6,
        "cache_creation_input_token_cost_above_1hr": 4e-6,
    },
    "claude-sonnet-4-6": {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 15e-6,
        "cache_read_input_token_cost": 0.3e-6,
        "cache_creation_input_token_cost": 3.75e-6,
        "cache_creation_input_token_cost_above_1hr": 6e-6,
    },
    "claude-haiku-4-5": {
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 5e-6,
        "cache_read_input_token_cost": 0.1e-6,
        "cache_creation_input_token_cost": 1.25e-6,
        "cache_creation_input_token_cost_above_1hr": 2e-6,
    },
}

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
_CACHE_TTL_SECONDS = 86_400  # 24 hours

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
    """
    Strip provider-specific prefixes/suffixes to get a litellm-compatible key.

    Examples:
      us.anthropic.claude-sonnet-4-5-20250929-v1:0  ->  claude-sonnet-4-5
      claude-sonnet-4-5@20250929                    ->  claude-sonnet-4-5
      claude-sonnet-4-6                             ->  claude-sonnet-4-6 (unchanged)
    """
    # Bedrock: remove leading region prefix like "us.anthropic." or "eu.anthropic."
    model = re.sub(r"^(?:us|eu|ap)\.anthropic\.", "", model)
    # Bedrock: remove trailing version suffix like "-20250929-v1:0"
    model = re.sub(r"-\d{8}.*$", "", model)
    # Vertex AI: remove revision suffix like "@20250929"
    model = re.sub(r"@\d{8}$", "", model)
    return model


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Estimate cost in USD using the litellm pricing database.

    Tries exact key first, then the normalised model name.
    Returns 0.0 if no pricing entry is found.
    """
    pricing = _get_litellm_pricing()
    entry = pricing.get(model) or pricing.get(normalize_model_name(model))
    if not entry:
        return 0.0
    in_rate = float(entry.get("input_cost_per_token", 0.0))
    out_rate = float(entry.get("output_cost_per_token", 0.0))
    return in_rate * input_tokens + out_rate * output_tokens


# Fallback used only when the remote fetch fails and the cache is empty.
_FALLBACK_PRICING: dict[str, Any] = {
    "claude-opus-4-6": {"input_cost_per_token": 15e-6, "output_cost_per_token": 75e-6},
    "claude-sonnet-4-6": {"input_cost_per_token": 3e-6, "output_cost_per_token": 15e-6},
    "claude-sonnet-4-5": {"input_cost_per_token": 3e-6, "output_cost_per_token": 15e-6},
    "claude-sonnet-4-20250514": {"input_cost_per_token": 3e-6, "output_cost_per_token": 15e-6},
    "claude-haiku-4-5": {"input_cost_per_token": 0.8e-6, "output_cost_per_token": 4e-6},
}

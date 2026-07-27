"""Shared helpers for the Ollama providers.

Ollama serves both wire formats a provider family needs, so it backs two
providers: `OllamaProvider` (OpenAI-compatible, drives codex) and
`OllamaClaudeProvider` (Anthropic-compatible, drives the Claude agent).
Everything that does not depend on the wire format lives here.
"""

from __future__ import annotations

import logging

import requests

from openscientist.models import _DEFAULT_CONTEXT_TOKENS, ModelProfile
from openscientist.providers.base import CostInfo
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def ollama_http_base(base_url: str) -> str:
    """The Ollama HTTP root from its OpenAI-compatible base URL.

    ``OLLAMA_BASE_URL`` is the OpenAI-compat endpoint (``.../v1``). The native
    ``/api/*`` routes, and the Anthropic-compatible ``/v1/messages`` route the
    Claude SDK builds for itself, both hang off the root one level up.
    """
    return base_url.rstrip("/").removesuffix("/v1").rstrip("/")


def probe_ollama_context_tokens(base_url: str, model_id: str) -> int | None:
    """Read the actual runtime context window of a loaded Ollama model.

    ``/api/ps`` reports ``context_length`` for currently-loaded models, which
    reflects the deployment's ``num_ctx`` (e.g. ``OLLAMA_CONTEXT_LENGTH``), the
    number we must budget against. Falls back to ``/api/show`` (the model's
    trained maximum) when the model is not currently loaded. Returns None on any
    failure so the caller can fall back further.
    """
    root = ollama_http_base(base_url)
    try:
        resp = requests.get(f"{root}/api/ps", timeout=5)
        resp.raise_for_status()
        for m in resp.json().get("models", []):
            name = m.get("name", "")
            if (name == model_id or name.startswith(model_id)) and m.get("context_length"):
                return int(m["context_length"])
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.debug("Ollama /api/ps probe failed: %s", exc)

    try:
        resp = requests.post(f"{root}/api/show", json={"name": model_id}, timeout=5)
        resp.raise_for_status()
        info = resp.json().get("model_info", {})
        for key, value in info.items():
            if key.endswith("context_length") and value:
                return int(value)
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.debug("Ollama /api/show probe failed: %s", exc)

    return None


def ollama_cost_info(display_name: str, lookback_hours: int) -> CostInfo:
    """Zero-spend cost report for local inference."""
    # Local inference has no per-call API cost. Report zero spend so the
    # budget checks pass cleanly rather than warning on unavailable data.
    return CostInfo(
        provider_name=display_name,
        total_spend_usd=0.0,
        recent_spend_usd=0.0,
        recent_period_hours=lookback_hours,
        data_lag_note="Local Ollama inference incurs no API cost.",
    )


def ollama_model_profile(model_id: str) -> ModelProfile:
    """The context window to budget prompts against for a local model."""
    # A self-hosted window is whatever num_ctx the deployment allocates, so
    # probe the live server. Order: explicit override, live probe, default.
    # The known-model table is skipped (a trained maximum would over-budget
    # a deployment served at a smaller num_ctx).
    s = get_settings().provider
    if s.model_context_tokens:
        return ModelProfile(id=model_id, context_window_tokens=int(s.model_context_tokens))
    probed = probe_ollama_context_tokens(s.ollama_base_url, model_id)
    if probed:
        return ModelProfile(id=model_id, context_window_tokens=probed)
    # A failed probe is NOT silent: it collapses the prompt budget to the
    # conservative default, which over-trims the report's literature. Surface
    # it so an operator can pin the window instead of shipping a thin report.
    logger.warning(
        "Could not probe the Ollama context window for %s; falling back to a "
        "%d-token budget, so the report prompt will be trimmed more than "
        "necessary. Set OPENSCIENTIST_MODEL_CONTEXT_TOKENS to pin the window.",
        model_id,
        _DEFAULT_CONTEXT_TOKENS,
    )
    return ModelProfile(id=model_id, context_window_tokens=_DEFAULT_CONTEXT_TOKENS)

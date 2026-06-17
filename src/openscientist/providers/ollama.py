"""Ollama provider (drives the Codex agent against a local model).

Routes the Codex agent at a locally hosted Ollama server through its
OpenAI-compatible Responses endpoint (default
``http://localhost:11434/v1``), which serves open-weight models such as
``gpt-oss:20b`` with tool calling. Ollama is local and keyless, so codex
is told the provider needs no OpenAI auth (``requires_openai_auth =
false``) and no API key is sent.

Because the server runs on the host, the base URL must be reachable from
wherever the agent runs. In-process on the dev box ``localhost`` works
directly. From inside the agent container, point ``OLLAMA_BASE_URL`` at
the host (for example ``http://host.docker.internal:11434/v1``).
"""

from __future__ import annotations

import logging

import requests

from openscientist.models import _DEFAULT_CONTEXT_TOKENS, ModelProfile
from openscientist.providers.base import CodexCompatible, CostInfo
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def _ollama_http_base(base_url: str) -> str:
    """The Ollama HTTP root from its OpenAI-compatible base URL.

    ``OLLAMA_BASE_URL`` is the OpenAI-compat endpoint (``.../v1``). The native
    ``/api/*`` routes live one level up.
    """
    return base_url.rstrip("/").removesuffix("/v1").rstrip("/")


def _probe_ollama_context_tokens(base_url: str, model_id: str) -> int | None:
    """Read the actual runtime context window of a loaded Ollama model.

    ``/api/ps`` reports ``context_length`` for currently-loaded models, which
    reflects the deployment's ``num_ctx`` (e.g. ``OLLAMA_CONTEXT_LENGTH``), the
    number we must budget against. Falls back to ``/api/show`` (the model's
    trained maximum) when the model is not currently loaded. Returns None on any
    failure so the caller can fall back further.
    """
    root = _ollama_http_base(base_url)
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


class OllamaProvider(CodexCompatible):
    """Local Ollama server as a Codex backend (open-weight models)."""

    @property
    def id(self) -> str:
        return "ollama"

    @property
    def display_name(self) -> str:
        return "Ollama (local)"

    def validate_required_config(self) -> list[str]:
        # Local and keyless: the base URL and model both have defaults, so
        # there is nothing the operator must supply for the provider to
        # construct. Reachability is a runtime concern, surfaced by the run.
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # Local inference has no per-call API cost. Report zero spend so the
        # budget checks pass cleanly rather than warning on unavailable data.
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=0.0,
            recent_spend_usd=0.0,
            recent_period_hours=lookback_hours,
            data_lag_note="Local Ollama inference incurs no API cost.",
        )

    def codex_config_overrides(self) -> list[str]:
        # The id is "ollama-local", not "ollama", because codex reserves
        # "ollama" as a built-in provider we cannot override to set our own
        # base_url (e.g. host.docker.internal from a container). Ollama needs no
        # auth, and codex only supports wire_api = "responses".
        return [
            "[model_providers.ollama-local]",
            'name = "Ollama (local)"',
            f'base_url = "{get_settings().provider.ollama_base_url}"',
            'wire_api = "responses"',
            "requires_openai_auth = false",
            # A CPU-offloaded model can stay silent for minutes during prefill
            # before the first SSE token, tripping codex's default 5-minute idle
            # timeout. Raise it to 1 hour, with a few reconnects as insurance.
            "stream_idle_timeout_ms = 3600000",
            "stream_max_retries = 5",
        ]

    def codex_model_name(self) -> str | None:
        # Default to the configured Ollama model unless OPENSCIENTIST_MODEL is set.
        s = get_settings().provider
        return s.model or s.ollama_model

    def model_profile(self) -> ModelProfile:
        # A self-hosted window is whatever num_ctx the deployment allocates, so
        # probe the live server. Order: explicit override, live probe, default.
        # The known-model table is skipped (a trained maximum would over-budget
        # a deployment served at a smaller num_ctx).
        s = get_settings().provider
        model_id = self.effective_model_name() or "unknown"
        if s.model_context_tokens:
            return ModelProfile(id=model_id, context_window_tokens=int(s.model_context_tokens))
        probed = _probe_ollama_context_tokens(s.ollama_base_url, model_id)
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

    def codex_model_provider_id(self) -> str:
        # Not "ollama": codex reserves that id for its built-in provider.
        return "ollama-local"

    def codex_sdk_env(self) -> dict[str, str]:
        # Keyless: nothing to forward into the codex child environment.
        return {}

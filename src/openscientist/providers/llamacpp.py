"""llama.cpp provider. A self-hosted ``llama-server`` on the OpenAI wire, driven by omp.

Not a Codex backend, since Codex's tool calls fail against non-gptoss open models.
"""

from __future__ import annotations

import logging

import requests

from openscientist.providers.base import SelfHostedOpenAiWire
from openscientist.settings import ProviderSettings

logger = logging.getLogger(__name__)


def _probe_llamacpp_context_tokens(base_url: str, api_key: str | None) -> int | None:
    """Read the launched window from ``/props`` ``default_generation_settings.n_ctx``.

    The trained maximum from ``/v1/models`` ``n_ctx_train`` over-budgets a server
    launched smaller, so the probe reads the launched value. ``/props`` sits at
    the server root.
    """
    root = base_url.rstrip("/").removesuffix("/v1").rstrip("/")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(f"{root}/props", headers=headers, timeout=5)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.debug("llama.cpp /props probe failed: %s", exc)
        return None

    gen = payload.get("default_generation_settings") if isinstance(payload, dict) else None
    if not isinstance(gen, dict):
        return None
    try:
        window = int(gen.get("n_ctx") or 0)
    except (TypeError, ValueError):
        logger.debug("llama.cpp reported a non-numeric n_ctx")
        return None
    return window or None


class LlamaCppProvider(SelfHostedOpenAiWire):
    """Self-hosted llama.cpp server, driven by omp."""

    @property
    def id(self) -> str:
        return "llamacpp"

    display_name = "llama.cpp (self-hosted)"
    server_name = "llama.cpp"
    base_url_env = "LLAMACPP_BASE_URL"
    api_key_env = "LLAMACPP_API_KEY"

    @classmethod
    def _base_url_of(cls, provider: ProviderSettings) -> str:
        return provider.llamacpp_base_url

    @classmethod
    def _api_key_of(cls, provider: ProviderSettings) -> str | None:
        return provider.llamacpp_api_key

    @staticmethod
    def _probe_context_tokens(base_url: str, model_id: str, api_key: str | None) -> int | None:
        # /props is server-global, so model_id is unused (unlike vLLM's per-card probe).
        return _probe_llamacpp_context_tokens(base_url, api_key)

"""vLLM provider (self-hosted open-weight models on an OpenAI-compatible wire).

Driven by omp, not by Codex. Codex's tool calls fail against non-gptoss open
models. From inside the agent container, point ``VLLM_BASE_URL`` at the host
(for example ``http://host.docker.internal:8000/v1``).
"""

from __future__ import annotations

import logging

import requests

from openscientist.providers.base import SelfHostedOpenAiWire
from openscientist.settings import ProviderSettings

logger = logging.getLogger(__name__)


def _probe_vllm_context_tokens(base_url: str, model_id: str, api_key: str | None) -> int | None:
    """Read the served context window from vLLM's ``GET /v1/models``.

    vLLM fixes the window at launch (``--max-model-len``) and reports it as
    ``max_model_len`` on every model card. A server started with ``--api-key``
    rejects an unauthenticated probe, so the credential is required whenever one
    is in play.
    """
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=5)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.debug("vLLM /v1/models probe failed: %s", exc)
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    cards = [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []
    card = next((c for c in cards if c.get("id") == model_id), None)
    # A one-model server is the common deployment, so its single card answers
    # the question even when the configured id spells the model differently.
    if card is None and len(cards) == 1:
        card = cards[0]
    if card is None:
        return None
    try:
        window = int(card.get("max_model_len") or 0)
    except (TypeError, ValueError):
        logger.debug("vLLM reported a non-numeric max_model_len for %s", model_id)
        return None
    return window or None


class VllmProvider(SelfHostedOpenAiWire):
    """Self-hosted vLLM server driven by omp (open-weight models)."""

    @property
    def id(self) -> str:
        return "vllm"

    display_name = "vLLM (self-hosted)"
    server_name = "vLLM"
    base_url_env = "VLLM_BASE_URL"
    api_key_env = "VLLM_API_KEY"

    @classmethod
    def _base_url_of(cls, provider: ProviderSettings) -> str:
        return provider.vllm_base_url

    @classmethod
    def _api_key_of(cls, provider: ProviderSettings) -> str | None:
        return provider.vllm_api_key

    @staticmethod
    def _probe_context_tokens(base_url: str, model_id: str, api_key: str | None) -> int | None:
        return _probe_vllm_context_tokens(base_url, model_id, api_key)

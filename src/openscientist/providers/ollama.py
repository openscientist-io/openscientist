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

To drive the same server with the Claude agent instead, see the sibling
``ollama_claude`` provider, which uses Ollama's Anthropic-compatible route.
"""

from __future__ import annotations

import logging
import os

from openscientist.models import ModelProfile
from openscientist.providers.base import LLM_PROXY_URL_ENV, CodexCompatible, CostInfo, LlmUpstream
from openscientist.settings import get_settings

from ._ollama_common import ollama_cost_info, ollama_model_profile

logger = logging.getLogger(__name__)


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
        return ollama_cost_info(self.display_name, lookback_hours)

    def llm_upstream(self) -> LlmUpstream | None:
        # Keyless: forward to Ollama with no injected auth.
        return LlmUpstream(get_settings().provider.ollama_base_url, {})

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        # Codex sends the placeholder so the proxy authenticates the container.
        return {"OPENAI_API_KEY": placeholder, LLM_PROXY_URL_ENV: proxy_base_url}

    def codex_config_overrides(self) -> list[str]:
        # id is "ollama-local", not "ollama": codex reserves "ollama" as a
        # built-in provider we cannot repoint at our own base_url.
        proxy = os.environ.get(LLM_PROXY_URL_ENV)
        lines = [
            "[model_providers.ollama-local]",
            'name = "Ollama (local)"',
            f'base_url = "{proxy or get_settings().provider.ollama_base_url}"',
            'wire_api = "responses"',
            f"requires_openai_auth = {'true' if proxy else 'false'}",
        ]
        if proxy:
            lines.append('env_key = "OPENAI_API_KEY"')
        # A CPU-offloaded model can stay silent for minutes during prefill before
        # the first SSE token, tripping codex's default 5-minute idle timeout.
        # Raise it to 1 hour, with a few reconnects as insurance.
        lines += ["stream_idle_timeout_ms = 3600000", "stream_max_retries = 5"]
        return lines

    def codex_model_name(self) -> str | None:
        # Default to the configured Ollama model unless OPENSCIENTIST_MODEL is set.
        s = get_settings().provider
        return s.model or s.ollama_model

    def model_profile(self) -> ModelProfile:
        return ollama_model_profile(self.effective_model_name() or "unknown")

    def codex_model_provider_id(self) -> str:
        # Not "ollama": codex reserves that id for its built-in provider.
        return "ollama-local"

    def codex_sdk_env(self) -> dict[str, str]:
        # Keyless: nothing to forward into the codex child environment.
        return {}

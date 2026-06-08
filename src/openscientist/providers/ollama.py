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

from openscientist.providers.base import CodexCompatible, CostInfo
from openscientist.settings import get_settings


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
        # A [model_providers.ollama-local] TOML table. The id is "ollama-local"
        # rather than "ollama" because codex reserves "ollama" as a built-in
        # provider that cannot be overridden, and we need to set our own
        # base_url (e.g. host.docker.internal from a container). No env_key:
        # Ollama needs no auth, and requires_openai_auth = false tells codex
        # not to demand an OpenAI login or API key. Codex only supports
        # wire_api = "responses" (the chat wire was removed).
        return [
            "[model_providers.ollama-local]",
            'name = "Ollama (local)"',
            f'base_url = "{get_settings().provider.ollama_base_url}"',
            'wire_api = "responses"',
            "requires_openai_auth = false",
            # A large CPU-offloaded model (gpt-oss:120b) can stay silent for many
            # minutes while it prefills a growing context before emitting the
            # first SSE token. Codex's default 5-minute stream idle timeout then
            # drops the connection mid-run ("idle timeout waiting for SSE").
            # Raise it to 1 hour so a slow prefill is not mistaken for a dead
            # stream, and allow a few reconnection attempts as insurance.
            "stream_idle_timeout_ms = 3600000",
            "stream_max_retries = 5",
        ]

    def codex_model_name(self) -> str | None:
        # Default to the configured Ollama model unless OPENSCIENTIST_MODEL is set.
        s = get_settings().provider
        return s.model or s.ollama_model

    def codex_model_provider_id(self) -> str:
        # Not "ollama": codex reserves that id for its built-in provider.
        return "ollama-local"

    def codex_sdk_env(self) -> dict[str, str]:
        # Keyless: nothing to forward into the codex child environment.
        return {}

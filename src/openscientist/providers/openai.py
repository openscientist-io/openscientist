"""Direct OpenAI API provider (drives the Codex agent).

Auth is ``OPENAI_API_KEY`` or the ChatGPT OAuth login the codex CLI stores
in ``~/.codex/auth.json``.
"""

from __future__ import annotations

import os
from pathlib import Path

from openscientist.providers.base import (
    LLM_PROXY_URL_ENV,
    AirgapEgress,
    AirgapPosture,
    CodexCompatible,
    CostInfo,
    LlmUpstream,
    env_from_pairs,
)
from openscientist.settings import ProviderSettings, get_settings

_OPENAI_BASE_URL = "https://api.openai.com/v1"


def _codex_auth_json() -> Path:
    """Path to the codex CLI's stored OAuth login (default codex home)."""
    return Path.home() / ".codex" / "auth.json"


class OpenAIDirectProvider(CodexCompatible):
    """OpenAI's API as a Codex backend (Codex's default endpoint)."""

    @property
    def id(self) -> str:
        return "openai"

    display_name = "OpenAI API"

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        return env_from_pairs(
            [
                ("OPENAI_API_KEY", provider.openai_api_key),
                ("CODEX_AUTH_HOST_PATH", provider.codex_auth_host_path),
            ]
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        # Auth is satisfied by an API key, a configured codex auth file (which
        # the container runner provisions into the per-job CODEX_HOME), or a
        # local codex CLI login.
        if (
            os.environ.get("OPENAI_API_KEY")
            or provider.codex_auth_host_path
            or _codex_auth_json().exists()
        ):
            return []
        return [
            "OpenAI provider needs auth: set OPENAI_API_KEY, set "
            "CODEX_AUTH_HOST_PATH, or log in with the codex CLI ('codex login') "
            "so ~/.codex/auth.json exists."
        ]

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # OpenAI exposes no simple per-key spend endpoint, so report unavailable.
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
            data_lag_note="OpenAI per-key cost tracking is not available.",
        )

    def llm_upstream(self) -> LlmUpstream | None:
        key = get_settings().provider.openai_api_key
        if key:
            return LlmUpstream(_OPENAI_BASE_URL, {"authorization": f"Bearer {key}"})
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        if get_settings().provider.openai_api_key:
            return {"OPENAI_API_KEY": placeholder, LLM_PROXY_URL_ENV: proxy_base_url}
        return {}

    def airgap_egress(self) -> AirgapPosture:
        if get_settings().provider.openai_api_key:
            return AirgapPosture(AirgapEgress.PROXY)
        return AirgapPosture(
            AirgapEgress.UNSUPPORTED,
            reason=(
                "OpenAI codex ChatGPT-login cannot be air-gapped: its model turn "
                "targets chatgpt.com over a websocket that no base-url config "
                "redirects. Set OPENAI_API_KEY for a proxied, air-gappable setup."
            ),
        )

    def codex_config_overrides(self) -> list[str]:
        # Codex ships a built-in "openai" entry at the default endpoint. When the
        # proxy is active, override base_url so codex routes through it.
        proxy = os.environ.get(LLM_PROXY_URL_ENV)
        if not proxy:
            return []
        return [
            "[model_providers.openai]",
            'name = "OpenAI"',
            f'base_url = "{proxy}"',
            'env_key = "OPENAI_API_KEY"',
            'wire_api = "responses"',
        ]

    def codex_model_name(self) -> str | None:
        # No forced default. codex uses its account or config default unless the
        # operator sets OPENSCIENTIST_MODEL (some accounts reject explicit ids).
        return get_settings().provider.model

    def codex_model_provider_id(self) -> str:
        return "openai"

    def codex_sdk_env(self) -> dict[str, str]:
        key = os.environ.get("OPENAI_API_KEY")
        return {"OPENAI_API_KEY": key} if key else {}

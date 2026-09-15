"""Azure OpenAI Service provider (drives the Codex agent).

Routes the Codex agent at an Azure-hosted OpenAI deployment. On Azure's
OpenAI v1 surface the deployment name is the request-body model, so
``OPENSCIENTIST_MODEL`` names it.
Authentication is an ``AZURE_OPENAI_API_KEY`` sent as a Bearer token.

This is distinct from ``FoundryProvider``, which is ``ClaudeCompatible`` and
serves Anthropic models through Azure AI Foundry.
"""

from __future__ import annotations

import os

from openscientist.providers.base import (
    LLM_PROXY_URL_ENV,
    CodexCompatible,
    CostInfo,
    LlmUpstream,
    env_from_pairs,
)
from openscientist.settings import ProviderSettings, get_settings


class AzureOpenAIProvider(CodexCompatible):
    """Azure OpenAI Service as a Codex backend (OpenAI models via Azure)."""

    @property
    def id(self) -> str:
        return "azure-openai"

    display_name = "Azure OpenAI Service"

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        return env_from_pairs(
            [
                ("AZURE_OPENAI_API_KEY", provider.azure_openai_api_key),
                ("AZURE_OPENAI_RESOURCE", provider.azure_openai_resource),
                ("AZURE_OPENAI_API_VERSION", provider.azure_openai_api_version),
                ("AZURE_OPENAI_STREAM_MAX_RETRIES", str(provider.azure_openai_stream_max_retries)),
            ]
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        errors: list[str] = []
        if not os.environ.get("AZURE_OPENAI_API_KEY"):
            errors.append("AZURE_OPENAI_API_KEY is required for the Azure OpenAI provider.")
        if not provider.azure_openai_resource:
            errors.append(
                "AZURE_OPENAI_RESOURCE is required (the <resource> in "
                "https://<resource>.openai.azure.com)."
            )
        if not provider.model:
            errors.append(
                "OPENSCIENTIST_MODEL is required (the Azure deployment name, which "
                "Azure routes on as the request-body model)."
            )
        return errors

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # Azure OpenAI spend is tracked through Azure Cost Management, which the
        # foundry provider already wires up. Report unavailable here.
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
            data_lag_note="Azure OpenAI per-key cost tracking is not available.",
        )

    def _base_url(self) -> str:
        # Azure serves the Responses API on the OpenAI v1 surface, not under
        # /deployments/<name>/. Codex appends "/responses" to base_url.
        return f"https://{get_settings().provider.azure_openai_resource}.openai.azure.com/openai/v1"

    def llm_upstream(self) -> LlmUpstream | None:
        s = get_settings().provider
        if s.azure_openai_api_key and s.azure_openai_resource:
            return LlmUpstream(
                self._base_url(), {"authorization": f"Bearer {s.azure_openai_api_key}"}
            )
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        s = get_settings().provider
        if s.azure_openai_api_key and s.azure_openai_resource:
            return {"AZURE_OPENAI_API_KEY": placeholder, LLM_PROXY_URL_ENV: proxy_base_url}
        return {}

    def codex_config_overrides(self) -> list[str]:
        # A [model_providers.azure-openai] TOML table. The key is sent as a
        # Bearer token (env_key). api-version is optional on the v1 surface, so
        # it is pinned only when the operator configures one.
        s = get_settings().provider
        # Codex only supports wire_api = "responses" (the chat wire was removed).
        # stream_max_retries makes codex reconnect through Azure's intermittent
        # streaming disconnects (a known Azure-side timeout, openai/codex#9936),
        # which it otherwise treats as a fatal "stream disconnected" error.
        base_url = os.environ.get(LLM_PROXY_URL_ENV) or self._base_url()
        lines = [
            "[model_providers.azure-openai]",
            'name = "Azure OpenAI Service"',
            f'base_url = "{base_url}"',
            'env_key = "AZURE_OPENAI_API_KEY"',
            'wire_api = "responses"',
            f"stream_max_retries = {s.azure_openai_stream_max_retries}",
        ]
        if s.azure_openai_api_version:
            lines.append(f'query_params = {{ "api-version" = "{s.azure_openai_api_version}" }}')
        return lines

    def codex_model_name(self) -> str | None:
        # Azure routes on the deployment name sent as the request-body model.
        return get_settings().provider.model

    def codex_model_provider_id(self) -> str:
        return "azure-openai"

    def codex_sdk_env(self) -> dict[str, str]:
        key = os.environ.get("AZURE_OPENAI_API_KEY")
        return {"AZURE_OPENAI_API_KEY": key} if key else {}

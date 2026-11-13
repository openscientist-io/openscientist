"""Azure OpenAI Service provider (drives the Codex agent).

Distinct from ``FoundryProvider``, which is ``ClaudeCompatible`` and serves
Anthropic models through Azure AI Foundry.
"""

from __future__ import annotations

import os
import re

from openscientist.providers.base import (
    LLM_PROXY_URL_ENV,
    AirgapEgress,
    AirgapPosture,
    CodexCompatible,
    CostInfo,
    LiveCheckResult,
    LlmUpstream,
    SetupQuestion,
    StaticHeadersSigning,
    _listing_live_check,
    env_from_pairs,
)
from openscientist.settings import ProviderSettings, get_settings

# A dated version such as 2025-04-01-preview, which the v1 surface refuses.
_DATED_API_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}")


class AzureOpenAIProvider(CodexCompatible):
    """Azure OpenAI Service as a Codex backend (OpenAI models via Azure)."""

    @property
    def id(self) -> str:
        return "azure-openai"

    display_name = "Azure OpenAI Service"
    credential_env_keys = ("AZURE_OPENAI_API_KEY",)

    @classmethod
    def container_env(cls, provider: ProviderSettings) -> dict[str, str]:
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
        version = provider.azure_openai_api_version
        if version and _DATED_API_VERSION.match(version):
            errors.append(
                f"AZURE_OPENAI_API_VERSION={version} is a dated version, which Azure's "
                "v1 surface refuses with HTTP 400 (API version not supported). Remove it, "
                "or set it to preview for preview features."
            )
        return errors

    @classmethod
    def setup_questions(cls) -> tuple[SetupQuestion, ...]:
        return (
            SetupQuestion("AZURE_OPENAI_API_KEY", kind="secret"),
            SetupQuestion(
                "AZURE_OPENAI_RESOURCE",
                help="the <resource> in https://<resource>.openai.azure.com",
            ),
            SetupQuestion("OPENSCIENTIST_MODEL", help="the Azure deployment name"),
        )

    @classmethod
    def _live_check(cls, p: ProviderSettings) -> LiveCheckResult:
        if not p.azure_openai_resource or not p.azure_openai_api_key:
            return LiveCheckResult(
                ok=True, detail="No Azure OpenAI resource or key to check with; skipped."
            )
        # The same v1 surface and query parameters the codex harness uses.
        url = f"https://{p.azure_openai_resource}.openai.azure.com/openai/v1/models"
        if p.azure_openai_api_version:
            url += f"?api-version={p.azure_openai_api_version}"
        headers = {"api-key": p.azure_openai_api_key}
        return _listing_live_check(url, headers, cls.display_name)

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # Azure OpenAI spend is tracked through Azure Cost Management, which the
        # foundry provider already wires up. Report unavailable here.
        return CostInfo.unavailable(
            self.display_name,
            "Azure OpenAI per-key cost tracking is not available.",
            lookback_hours,
        )

    def _base_url(self) -> str:
        # Azure serves the Responses API on the OpenAI v1 surface, not under
        # /deployments/<name>/. Codex appends "/responses" to base_url.
        return f"https://{get_settings().provider.azure_openai_resource}.openai.azure.com/openai/v1"

    def llm_upstream(self) -> LlmUpstream | None:
        s = get_settings().provider
        if s.azure_openai_api_key and s.azure_openai_resource:
            return LlmUpstream(
                self._base_url(),
                StaticHeadersSigning({"authorization": f"Bearer {s.azure_openai_api_key}"}),
            )
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        s = get_settings().provider
        if s.azure_openai_api_key and s.azure_openai_resource:
            return {"AZURE_OPENAI_API_KEY": placeholder, LLM_PROXY_URL_ENV: proxy_base_url}
        return {}

    @classmethod
    def airgap_posture(cls, provider: ProviderSettings) -> AirgapPosture:
        if provider.azure_openai_api_key and provider.azure_openai_resource:
            return AirgapPosture(AirgapEgress.PROXY)
        return AirgapPosture(
            AirgapEgress.UNSUPPORTED,
            reason=f"{cls.display_name} cannot be air-gapped.",
        )

    def codex_config_overrides(self) -> list[str]:
        # api-version is optional on the v1 surface, so pin it only when configured.
        s = get_settings().provider
        # Codex only supports wire_api = "responses".
        # stream_max_retries makes codex reconnect through Azure's intermittent
        # streaming disconnects (https://github.com/openai/codex/issues/9936).
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

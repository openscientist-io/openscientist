"""AWS Bedrock OpenAI provider (drives the Codex agent).

Routes the Codex agent at Amazon Bedrock's OpenAI-compatible Responses API
(the "Mantle" endpoint, ``https://bedrock-mantle.<region>.api.aws/v1``), which
serves OpenAI's open-weight gpt-oss models with tool calling. Authentication is
a Bedrock API key sent as a Bearer token.

This is distinct from ``BedrockProvider``, which is ``ClaudeCompatible`` and
serves Anthropic models through the Bedrock runtime.
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


class BedrockOpenAIProvider(CodexCompatible):
    """Amazon Bedrock as a Codex backend (OpenAI gpt-oss models via Mantle)."""

    @property
    def id(self) -> str:
        return "bedrock-openai"

    display_name = "AWS Bedrock OpenAI"

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        return env_from_pairs(
            [
                ("BEDROCK_API_KEY", provider.bedrock_api_key),
                ("BEDROCK_REGION", provider.bedrock_region),
                ("BEDROCK_MODEL", provider.bedrock_model),
                ("BEDROCK_STREAM_MAX_RETRIES", str(provider.bedrock_stream_max_retries)),
            ]
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        if not os.environ.get("BEDROCK_API_KEY"):
            return ["BEDROCK_API_KEY is required for the AWS Bedrock OpenAI provider."]
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # Bedrock spend is tracked through AWS Cost Explorer, not a per-key endpoint.
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
            data_lag_note="AWS Bedrock per-key cost tracking is not available.",
        )

    def _base_url(self) -> str:
        # Codex appends "/responses" to base_url and sends the model in the body.
        return f"https://bedrock-mantle.{get_settings().provider.bedrock_region}.api.aws/v1"

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        if proxy:
            return super().harness_env(proxy=proxy)
        # Mantle is not api.openai.com, so an unproxied harness needs the real endpoint.
        return env_from_pairs(
            [
                ("OPENAI_BASE_URL", self._base_url()),
                ("OPENAI_API_KEY", get_settings().provider.bedrock_api_key),
            ]
        )

    def llm_upstream(self) -> LlmUpstream | None:
        key = get_settings().provider.bedrock_api_key
        if not key:
            return None
        return LlmUpstream(self._base_url(), {"authorization": f"Bearer {key}"})

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        if not get_settings().provider.bedrock_api_key:
            return {}
        # Never ship the real key into the container.
        return {
            "BEDROCK_API_KEY": placeholder,
            "OPENAI_API_KEY": placeholder,
            LLM_PROXY_URL_ENV: proxy_base_url,
        }

    def codex_config_overrides(self) -> list[str]:
        # The key is sent as a Bearer token (env_key); stream_max_retries reconnects
        # through transient streaming disconnects rather than failing the turn.
        s = get_settings().provider
        base_url = os.environ.get(LLM_PROXY_URL_ENV) or self._base_url()
        return [
            "[model_providers.bedrock-openai]",
            'name = "AWS Bedrock OpenAI"',
            f'base_url = "{base_url}"',
            'env_key = "BEDROCK_API_KEY"',
            'wire_api = "responses"',
            f"stream_max_retries = {s.bedrock_stream_max_retries}",
        ]

    def codex_model_name(self) -> str | None:
        # The model id is sent in the request body.
        s = get_settings().provider
        return s.model or s.bedrock_model

    def codex_model_provider_id(self) -> str:
        return "bedrock-openai"

    def codex_sdk_env(self) -> dict[str, str]:
        key = os.environ.get("BEDROCK_API_KEY")
        return {"BEDROCK_API_KEY": key} if key else {}

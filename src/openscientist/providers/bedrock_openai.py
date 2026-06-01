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

from openscientist.providers.base import CodexCompatible, CostInfo
from openscientist.settings import get_settings


class BedrockOpenAIProvider(CodexCompatible):
    """Amazon Bedrock as a Codex backend (OpenAI gpt-oss models via Mantle)."""

    @property
    def id(self) -> str:
        return "bedrock-openai"

    @property
    def display_name(self) -> str:
        return "AWS Bedrock OpenAI"

    def validate_required_config(self) -> list[str]:
        if not os.environ.get("BEDROCK_API_KEY"):
            return ["BEDROCK_API_KEY is required for the AWS Bedrock OpenAI provider."]
        return []

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        # Bedrock spend is tracked through AWS Cost Explorer, not a per-key
        # endpoint, so report unavailable here.
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
            data_lag_note="AWS Bedrock per-key cost tracking is not available.",
        )

    def _base_url(self) -> str:
        # Bedrock serves the OpenAI Responses API on the Mantle endpoint. Codex
        # appends "/responses" to base_url and sends the model in the body.
        return f"https://bedrock-mantle.{get_settings().provider.bedrock_region}.api.aws/v1"

    def codex_config_overrides(self) -> list[str]:
        # A [model_providers.bedrock-openai] TOML table. The Bedrock API key is
        # sent as a Bearer token (env_key). stream_max_retries reconnects through
        # transient streaming disconnects rather than failing the turn.
        s = get_settings().provider
        return [
            "[model_providers.bedrock-openai]",
            'name = "AWS Bedrock OpenAI"',
            f'base_url = "{self._base_url()}"',
            'env_key = "BEDROCK_API_KEY"',
            'wire_api = "responses"',
            f"stream_max_retries = {s.bedrock_stream_max_retries}",
        ]

    def codex_model_name(self) -> str | None:
        # The model id is sent in the request body. Default to gpt-oss-120b
        # unless OPENSCIENTIST_MODEL or BEDROCK_MODEL is set.
        s = get_settings().provider
        return s.model or s.bedrock_model

    def codex_model_provider_id(self) -> str:
        return "bedrock-openai"

    def codex_sdk_env(self) -> dict[str, str]:
        key = os.environ.get("BEDROCK_API_KEY")
        return {"BEDROCK_API_KEY": key} if key else {}

"""
CBORG provider implementation.

Uses CBORG API for model access and cost tracking.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from shandy.exceptions import ProviderError
from shandy.providers.base import BaseProvider, CostInfo
from shandy.settings import get_settings

logger = logging.getLogger(__name__)


class CborgProvider(BaseProvider):
    """
    CBORG API provider (current implementation).

    KNOWN ISSUES:
    - Newer versions of Claude CLI may send headers that CBORG doesn't recognize,
      causing HTTP 400 errors. If you encounter authentication or request errors,
      check your Claude CLI version and consider using an older version if needed.
    """

    @property
    def name(self) -> str:
        return "CBORG"

    def _validate_required_config(self) -> list[str]:
        """Check required CBORG configuration."""
        errors = []
        settings = get_settings()

        if not settings.provider.anthropic_auth_token:
            errors.append("ANTHROPIC_AUTH_TOKEN not set (required for CBORG)")

        if not settings.provider.anthropic_base_url:
            errors.append("ANTHROPIC_BASE_URL not set (should be https://api.cborg.lbl.gov)")

        return errors

    def _validate_optional_config(self) -> list[str]:
        """Check optional CBORG configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.anthropic_model:
            warnings.append("ANTHROPIC_MODEL not set (will use Claude CLI default)")

        return warnings

    def setup_environment(self) -> None:
        """CBORG environment should be configured via .env and docker-compose.yml."""
        # Unset conflicting provider vars
        os.environ.pop("CLAUDE_CODE_USE_BEDROCK", None)  # noqa: env-ok
        os.environ.pop("CLAUDE_CODE_USE_VERTEX", None)  # noqa: env-ok
        os.environ.pop("ANTHROPIC_API_KEY", None)  # noqa: env-ok
        logger.info("CBORG provider initialized (configuration from environment)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get CBORG cost information.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with CBORG spend data
        """
        settings = get_settings()
        token = settings.provider.anthropic_auth_token
        if not token:
            raise ValueError("ANTHROPIC_AUTH_TOKEN not set")

        # Get total spend and budget from /key/info
        try:
            info_response = requests.get(
                "https://api.cborg.lbl.gov/key/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            info_response.raise_for_status()
            info = info_response.json()["info"]

            total_spend = info["spend"]
            max_budget = info.get("max_budget")
            key_expires = info.get("expires")

        except (requests.RequestException, KeyError, ValueError) as e:
            logger.error("Failed to fetch CBORG /key/info: %s", e)
            raise ProviderError(f"Failed to fetch CBORG /key/info: {e}") from e

        # Get recent spend from /user/daily/activity
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=lookback_hours)

            activity_response = requests.get(
                "https://api.cborg.lbl.gov/user/daily/activity",
                params={
                    "start_date": start_time.isoformat(),
                    "end_date": end_time.isoformat(),
                    "page": "1",
                    "page_size": "1000",
                },
                headers={"x-litellm-api-key": token},
                timeout=10,
            )
            activity_response.raise_for_status()

            # Sum up costs from activity records
            activity_data = activity_response.json().get("data", [])
            recent_spend = sum(record.get("spend", 0) for record in activity_data)

        except (requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Failed to fetch CBORG activity data: %s", e)
            # Fall back to 0 if activity endpoint fails
            recent_spend = 0.0

        # Calculate budget remaining
        budget_remaining = None
        if max_budget is not None:
            budget_remaining = max_budget - total_spend

        return CostInfo(
            provider_name="CBORG",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            budget_limit_usd=max_budget,
            budget_remaining_usd=budget_remaining,
            last_updated=datetime.now(timezone.utc),
            key_expires=key_expires,
        )

    async def send_message(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send message using CBORG via Anthropic SDK with custom base_url.

        This bypasses the Claude Code CLI and its local pre-flight content
        filter, which can produce false positives on legitimate scientific content.
        """
        import anthropic
        from anthropic.types import MessageParam, TextBlock

        settings = get_settings()
        client = anthropic.Anthropic(
            api_key=settings.provider.anthropic_auth_token,
            base_url=settings.provider.anthropic_base_url,
        )

        # Use configured model or default
        effective_model = model or settings.provider.anthropic_model or "claude-sonnet-4-20250514"

        # Convert to MessageParam type (role is validated elsewhere as user/assistant)
        typed_messages: list[MessageParam] = [
            {"role": msg["role"], "content": msg["content"]}  # type: ignore[typeddict-item]
            for msg in messages
        ]

        response = client.messages.create(
            model=effective_model,
            max_tokens=max_tokens,
            system=system or "",
            messages=typed_messages,
        )

        # Extract text from response (only TextBlock has .text)
        if response.content and len(response.content) > 0:
            first_block = response.content[0]
            if isinstance(first_block, TextBlock):
                return first_block.text
        return ""

    async def send_message_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """
        Send message with tool definitions using CBORG via Anthropic SDK.

        Returns full response including stop_reason and content blocks.
        """
        import anthropic
        from anthropic.types import ToolParam, ToolUseBlock

        settings = get_settings()
        client = anthropic.Anthropic(
            api_key=settings.provider.anthropic_auth_token,
            base_url=settings.provider.anthropic_base_url,
        )

        # Use configured model or default
        effective_model = model or settings.provider.anthropic_model or "claude-sonnet-4-20250514"

        # Convert tools to ToolParam format
        tool_params: list[ToolParam] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t["input_schema"],
            }
            for t in tools
        ]

        # Use block format for system prompt with cache_control
        # This enables prompt caching: 90% cost reduction, 85% latency improvement
        # Cache is "ephemeral" (5 minute TTL) - good for multi-turn agentic loops
        # Note: CBORG may or may not support caching depending on backend
        system_blocks = (
            [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            if system
            else []
        )

        response = client.messages.create(
            model=effective_model,
            max_tokens=max_tokens,
            system=system_blocks,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            tools=tool_params,
        )

        # Convert response to dict format
        content_blocks = []
        for block in response.content:
            if hasattr(block, "text"):
                content_blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        # Build usage dict with cache info if available
        usage: dict[str, int] = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        # Add cache metrics if present (from prompt caching)
        if hasattr(response.usage, "cache_creation_input_tokens"):
            usage["cache_creation_input_tokens"] = response.usage.cache_creation_input_tokens or 0
        if hasattr(response.usage, "cache_read_input_tokens"):
            usage["cache_read_input_tokens"] = response.usage.cache_read_input_tokens or 0

        return {
            "stop_reason": response.stop_reason,
            "content": content_blocks,
            "model": response.model,
            "usage": usage,
        }

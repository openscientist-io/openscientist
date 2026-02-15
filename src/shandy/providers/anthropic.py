"""
Direct Anthropic API provider.

For users who want to use their own Anthropic API key.
"""

import logging
import os
from typing import Any, List

from shandy.settings import get_settings

from .base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseProvider):
    """
    Direct Anthropic API provider.

    Uses ANTHROPIC_API_KEY for authentication directly with Anthropic's API.
    This is for users who want to bring their own personal API key.
    """

    @property
    def name(self) -> str:
        return "Anthropic"

    def _validate_required_config(self) -> List[str]:
        """Check required Anthropic configuration."""
        errors = []
        settings = get_settings()

        if not settings.provider.anthropic_api_key:
            errors.append(
                "ANTHROPIC_API_KEY not set. Get your API key from https://console.anthropic.com"
            )

        return errors

    def _validate_optional_config(self) -> List[str]:
        """Check optional configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.anthropic_model:
            warnings.append("ANTHROPIC_MODEL not set (will use Claude CLI default)")

        return warnings

    def setup_environment(self) -> None:
        """Anthropic environment is configured via ANTHROPIC_API_KEY in .env."""
        # Unset Vertex-related vars to ensure Claude Code uses ANTHROPIC_API_KEY
        os.environ.pop("CLAUDE_CODE_USE_VERTEX", None)  # noqa: env-ok
        os.environ.pop("ANTHROPIC_VERTEX_PROJECT_ID", None)  # noqa: env-ok
        logger.info("Anthropic provider initialized (using ANTHROPIC_API_KEY)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get cost information.

        Note: Anthropic's API doesn't provide a usage/billing endpoint,
        so we return unknown for cost tracking.
        """
        return CostInfo(
            provider_name=self.name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
            data_lag_note="Cost tracking not available for direct Anthropic API",
        )

    async def send_message(
        self,
        messages: List[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send message using Anthropic SDK directly.

        This bypasses the Claude Code CLI and its local pre-flight content
        filter, which can produce false positives on legitimate scientific content.
        """
        import anthropic
        from anthropic.types import MessageParam, TextBlock

        settings = get_settings()
        client = anthropic.Anthropic(api_key=settings.provider.anthropic_api_key)

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
        Send message with tool definitions using Anthropic SDK.

        Returns full response including stop_reason and content blocks.
        Uses prompt caching for the system prompt to reduce costs and latency.
        """
        import anthropic
        from anthropic.types import ToolParam, ToolUseBlock

        settings = get_settings()
        client = anthropic.Anthropic(api_key=settings.provider.anthropic_api_key)

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

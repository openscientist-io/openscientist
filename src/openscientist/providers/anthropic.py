"""
Direct Anthropic API provider.

For users who want to use their own Anthropic API key.
"""

import logging
import os
from typing import Any

from openscientist.settings import get_settings

from ._anthropic_common import (
    send_anthropic_message,
    send_anthropic_message_with_tools,
)
from ._env_cleanup import VERTEX_PROVIDER_ENV_VARS, clear_env_vars, clear_provider_mode_flags
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

    def _validate_required_config(self) -> list[str]:
        """Check required Anthropic configuration."""
        errors = []
        settings = get_settings()

        if (
            not settings.provider.anthropic_api_key
            and not settings.provider.claude_code_oauth_token
        ):
            errors.append(
                "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN not set. "
                "Get your API key from https://console.anthropic.com "
                "or run 'claude login' for OAuth."
            )

        return errors

    def _validate_optional_config(self) -> list[str]:
        """Check optional configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.anthropic_model:
            warnings.append("ANTHROPIC_MODEL not set (will use Claude CLI default)")

        return warnings

    def setup_environment(self) -> None:
        """Set up environment for Anthropic direct API or OAuth token."""
        settings = get_settings()

        # Unset conflicting provider routing vars
        clear_provider_mode_flags(logger)
        clear_env_vars(logger, VERTEX_PROVIDER_ENV_VARS)

        # If using OAuth token (from claude login), set CLAUDE_CODE_OAUTH_TOKEN
        # which is what the Claude Code CLI expects for OAuth authentication
        if settings.provider.claude_code_oauth_token and not settings.provider.anthropic_api_key:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = (
                settings.provider.claude_code_oauth_token
            )  # env-ok
            auth_method = "OAuth token (CLAUDE_CODE_OAUTH_TOKEN)"
        else:
            auth_method = "API key (ANTHROPIC_API_KEY)"

        logger.info("Anthropic provider initialized (using %s)", auth_method)

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
        messages: list[dict[str, str]],
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

        settings = get_settings()
        api_key = settings.provider.anthropic_api_key
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for direct SDK calls. "
                "OAuth tokens (CLAUDE_CODE_OAUTH_TOKEN) only work via the CLI path."
            )
        client = anthropic.Anthropic(api_key=api_key)
        return send_anthropic_message(
            client=client,
            messages=messages,
            system=system,
            model=model,
            configured_model=settings.provider.anthropic_model,
            provider_default_model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
        )

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
        from anthropic.types import ToolUseBlock

        settings = get_settings()
        api_key = settings.provider.anthropic_api_key
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for direct SDK calls. "
                "OAuth tokens (CLAUDE_CODE_OAUTH_TOKEN) only work via the CLI path."
            )
        client = anthropic.Anthropic(api_key=api_key)
        return send_anthropic_message_with_tools(
            client=client,
            messages=messages,
            tools=tools,
            system=system,
            model=model,
            configured_model=settings.provider.anthropic_model,
            provider_default_model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            tool_use_block_type=ToolUseBlock,
        )

"""
AWS Bedrock provider implementation.

Uses AWS Bedrock for model access. Cost tracking via AWS Cost Explorer.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from shandy.providers.base import BaseProvider, CostInfo
from shandy.settings import get_settings

from ._anthropic_common import (
    build_system_blocks,
    build_tool_params,
    build_usage_dict,
    convert_response_blocks,
)
from ._env_cleanup import (
    VERTEX_PROVIDER_ENV_VARS,
    clear_empty_env_vars,
    clear_env_vars,
    clear_provider_mode_flags,
)

logger = logging.getLogger(__name__)


class BedrockProvider(BaseProvider):
    """AWS Bedrock provider."""

    @property
    def name(self) -> str:
        return "AWS Bedrock"

    def _validate_required_config(self) -> list[str]:
        """Check required Bedrock configuration."""
        errors = []
        settings = get_settings()

        if not settings.provider.aws_region:
            errors.append("AWS_REGION not set (e.g., us-east-1)")

        # Check for at least one auth method
        has_access_key = (
            settings.provider.aws_access_key_id and settings.provider.aws_secret_access_key
        )
        has_profile = settings.provider.aws_profile
        has_bearer_token = settings.provider.aws_bearer_token_bedrock

        if not (has_access_key or has_profile or has_bearer_token):
            errors.append(
                "AWS credentials not configured. Set one of: "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, or AWS_BEARER_TOKEN_BEDROCK"
            )

        return errors

    def _validate_optional_config(self) -> list[str]:
        """Check optional Bedrock configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.anthropic_model:
            warnings.append(
                "ANTHROPIC_MODEL not set (will use global.anthropic.claude-sonnet-4-5-20250929-v1:0)"
            )

        if not settings.provider.anthropic_small_fast_model:
            warnings.append(
                "ANTHROPIC_SMALL_FAST_MODEL not set (will use us.anthropic.claude-haiku-4-5-20251001-v1:0)"
            )

        return warnings

    def setup_environment(self) -> None:
        """
        Set up environment for Bedrock.

        Ensures CLAUDE_CODE_USE_BEDROCK is set and unsets conflicting
        environment variables from other providers.
        """
        # Enable Bedrock mode for Claude Code
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"  # env-ok

        # Unset conflicting provider routing vars
        clear_provider_mode_flags(logger, active_flag="CLAUDE_CODE_USE_BEDROCK")
        clear_env_vars(logger, VERTEX_PROVIDER_ENV_VARS)

        # Unset direct API key to avoid conflicts
        clear_env_vars(logger, ("ANTHROPIC_API_KEY",))

        # Unset empty vars that interfere with Bedrock auth
        # This happens when docker-compose passes VAR=${VAR} and it's unset
        empty_vars_to_clear = [
            "AWS_PROFILE",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        ]
        clear_empty_env_vars(logger, empty_vars_to_clear)

        logger.info("Bedrock provider initialized (using AWS credentials)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get Bedrock cost information from AWS Cost Explorer.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with Bedrock spend data

        Note:
            Requires AWS Cost Explorer access. Cost data typically has
            a 24-48 hour lag in AWS.
        """
        now = datetime.now(UTC)

        # AWS Cost Explorer requires ce:GetCostAndUsage permission
        # For now, return unavailable status with instructions
        # Full implementation would use boto3 cost explorer client
        try:
            import boto3  # type: ignore[import-untyped]

            settings = get_settings()
            # Initialize Cost Explorer client
            ce_client = boto3.client("ce", region_name=settings.provider.aws_region or "us-east-1")

            # Calculate time windows (Cost Explorer requires date strings)
            end_date = now.strftime("%Y-%m-%d")
            start_date_recent = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
            # For total, go back 1 year (reasonable default)
            start_date_total = (now - timedelta(days=365)).strftime("%Y-%m-%d")

            # Query for Bedrock costs
            def get_bedrock_cost(start: str, end: str) -> float:
                response = ce_client.get_cost_and_usage(
                    TimePeriod={"Start": start, "End": end},
                    Granularity="DAILY",
                    Metrics=["UnblendedCost"],
                    Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
                )
                total = 0.0
                for result in response.get("ResultsByTime", []):
                    total += float(result["Total"]["UnblendedCost"]["Amount"])
                return total

            total_spend = get_bedrock_cost(start_date_total, end_date)
            recent_spend = get_bedrock_cost(start_date_recent, end_date)
            data_lag_note = "AWS billing data has 24-48 hour lag"

        except ImportError:
            logger.warning("boto3 not installed. Cannot fetch AWS cost data.")
            total_spend = None
            recent_spend = None
            data_lag_note = "boto3 not installed (pip install boto3)"

        except Exception as e:
            logger.warning(f"Could not fetch AWS cost data: {e}")
            total_spend = None
            recent_spend = None
            data_lag_note = f"Cost data unavailable: {e}"

        settings = get_settings()
        return CostInfo(
            provider_name="AWS Bedrock",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={"region": settings.provider.aws_region},
        )

    async def send_message(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send message using AWS Bedrock via Anthropic SDK.

        This bypasses the Claude Code CLI and its local pre-flight content
        filter, which can produce false positives on legitimate scientific content.
        """
        import anthropic
        from anthropic.types import MessageParam, TextBlock

        settings = get_settings()
        client = anthropic.AnthropicBedrock(
            aws_region=settings.provider.aws_region or "us-east-1",
        )

        # Use configured model or default (Bedrock uses different model IDs)
        effective_model = (
            model
            or settings.provider.anthropic_model
            or "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )

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
        Send message with tool definitions using AWS Bedrock via Anthropic SDK.

        Returns full response including stop_reason and content blocks.
        """
        import anthropic
        from anthropic.types import ToolParam, ToolUseBlock

        settings = get_settings()
        client = anthropic.AnthropicBedrock(
            aws_region=settings.provider.aws_region or "us-east-1",
        )

        # Use configured model or default (Bedrock uses different model IDs)
        effective_model = (
            model
            or settings.provider.anthropic_model
            or "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )

        # Convert tools to ToolParam format
        tool_params: list[ToolParam] = build_tool_params(tools)  # type: ignore[assignment]

        # Use block format for system prompt with cache_control
        # This enables prompt caching: 90% cost reduction, 85% latency improvement
        # Cache is "ephemeral" (5 minute TTL) - good for multi-turn agentic loops
        system_blocks = build_system_blocks(system)

        response = client.messages.create(
            model=effective_model,
            max_tokens=max_tokens,
            system=system_blocks,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            tools=tool_params,
        )

        # Convert response to dict format
        content_blocks = convert_response_blocks(
            response.content,
            tool_use_block_type=ToolUseBlock,
        )

        # Build usage dict with cache info if available
        usage = build_usage_dict(response.usage)

        return {
            "stop_reason": response.stop_reason,
            "content": content_blocks,
            "model": response.model,
            "usage": usage,
        }

"""
Azure AI Foundry (Microsoft Foundry) provider implementation.

Uses Azure AI Foundry for model access. Supports both API key and Entra ID authentication.
Cost tracking via Azure Cost Management API.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, List

from shandy.providers.base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class FoundryProvider(BaseProvider):
    """Azure AI Foundry provider."""

    @property
    def name(self) -> str:
        return "Azure AI Foundry"

    def _validate_required_config(self) -> List[str]:
        """Check required Foundry configuration."""
        errors = []

        # Resource name or base URL is required
        has_resource = os.getenv("ANTHROPIC_FOUNDRY_RESOURCE")  # noqa: env-ok
        has_base_url = os.getenv("ANTHROPIC_FOUNDRY_BASE_URL")  # noqa: env-ok

        if not (has_resource or has_base_url):
            errors.append(
                "Azure Foundry resource not configured. Set either "
                "ANTHROPIC_FOUNDRY_RESOURCE (resource name) or "
                "ANTHROPIC_FOUNDRY_BASE_URL (full endpoint URL)"
            )

        # Check authentication - either API key or Azure credentials
        # If API key is not set, we assume Azure default credential chain is available
        has_api_key = os.getenv("ANTHROPIC_FOUNDRY_API_KEY")  # noqa: env-ok
        if not has_api_key:
            logger.info(
                "ANTHROPIC_FOUNDRY_API_KEY not set. "
                "Will use Azure default credential chain (Entra ID authentication)"
            )

        return errors

    def _validate_optional_config(self) -> List[str]:
        """Check optional Foundry configuration."""
        warnings = []

        # Model deployment names (optional - Claude Code has defaults)
        if not os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"):  # noqa: env-ok
            warnings.append(
                "ANTHROPIC_DEFAULT_SONNET_MODEL not set "
                "(will use default deployment name 'claude-sonnet-4-5')"
            )

        if not os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"):  # noqa: env-ok
            warnings.append(
                "ANTHROPIC_DEFAULT_HAIKU_MODEL not set "
                "(will use default deployment name 'claude-haiku-4-5')"
            )

        if not os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"):  # noqa: env-ok
            warnings.append(
                "ANTHROPIC_DEFAULT_OPUS_MODEL not set "
                "(will use default deployment name 'claude-opus-4-6')"
            )

        return warnings

    def setup_environment(self) -> None:
        """
        Set up environment for Azure Foundry.

        Ensures CLAUDE_CODE_USE_FOUNDRY is set and unsets conflicting
        environment variables from other providers.
        """
        # Enable Foundry mode for Claude Code
        os.environ["CLAUDE_CODE_USE_FOUNDRY"] = "1"  # noqa: env-ok

        # Note: Claude Code 2.1.42+ will construct the base URL from ANTHROPIC_FOUNDRY_RESOURCE
        # automatically, so we don't need to set ANTHROPIC_FOUNDRY_BASE_URL here.
        # In fact, setting both causes an error: "baseURL and resource are mutually exclusive"

        # Unset Vertex-related vars to avoid conflicts
        vertex_vars = [
            "CLAUDE_CODE_USE_VERTEX",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "VERTEX_REGION_CLAUDE_4_5_SONNET",
            "VERTEX_REGION_CLAUDE_4_5_HAIKU",
        ]
        for var in vertex_vars:
            if var in os.environ:  # noqa: env-ok
                logger.debug(f"Removing conflicting {var}")
                del os.environ[var]  # noqa: env-ok

        # Unset Bedrock vars to avoid conflicts
        bedrock_vars = [
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_BEARER_TOKEN_BEDROCK",
        ]
        for var in bedrock_vars:
            if var in os.environ:  # noqa: env-ok
                logger.debug(f"Removing conflicting {var}")
                del os.environ[var]  # noqa: env-ok

        # Unset direct Anthropic API key to avoid conflicts
        os.environ.pop("ANTHROPIC_API_KEY", None)  # noqa: env-ok
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # noqa: env-ok

        # Unset empty vars that interfere with auth
        # This happens when docker-compose passes VAR=${VAR} and it's unset
        empty_vars_to_clear = [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "AWS_PROFILE",
            "AWS_SESSION_TOKEN",
        ]
        for var in empty_vars_to_clear:
            val = os.environ.get(var)  # noqa: env-ok
            if val == "":
                os.environ.pop(var, None)  # noqa: env-ok
                logger.debug(f"Unset empty {var}")

        auth_method = (
            "API key" if os.getenv("ANTHROPIC_FOUNDRY_API_KEY") else "Entra ID"  # noqa: env-ok
        )
        logger.info(f"Azure Foundry provider initialized (using {auth_method} authentication)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get Azure Foundry cost information from Azure Cost Management API.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with Foundry spend data

        Note:
            Requires Azure Cost Management API access.
            Cost data typically has a delay in Azure.
        """
        now = datetime.now(timezone.utc)

        # Azure Cost Management API requires authentication and proper permissions
        # For now, return unavailable status with instructions
        # Full implementation would use Azure SDK cost management client
        try:
            from azure.identity import DefaultAzureCredential  # type: ignore[import-untyped]
            from azure.mgmt.costmanagement import (  # type: ignore[import-untyped]
                CostManagementClient,
            )

            # Get subscription ID from environment or Azure context
            subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")  # noqa: env-ok
            if not subscription_id:
                raise ValueError(
                    "AZURE_SUBSCRIPTION_ID not set. Required for cost tracking. "
                    "Find your subscription ID in Azure Portal."
                )

            # Initialize Cost Management client
            credential = DefaultAzureCredential()
            _cost_client = CostManagementClient(credential, subscription_id)

            # Calculate time windows (prefixed with _ as not yet used)
            _end_date = now.strftime("%Y-%m-%d")
            _start_date_recent = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
            # For total, go back 90 days (reasonable default for Azure)
            _start_date_total = (now - timedelta(days=90)).strftime("%Y-%m-%d")

            # Query for Azure AI Foundry costs
            # Note: This is a simplified example - actual implementation would need
            # to filter by the specific Foundry resource and use proper query structure
            logger.warning(
                "Azure Cost Management API integration not fully implemented. "
                "Cost data unavailable."
            )
            total_spend = None
            recent_spend = None
            data_lag_note = (
                "Azure cost tracking not yet implemented. "
                "View costs in Azure Portal > Cost Management"
            )

        except ImportError:
            logger.warning(
                "Azure SDK not installed. Cannot fetch cost data. "
                "Install with: pip install azure-identity azure-mgmt-costmanagement"
            )
            total_spend = None
            recent_spend = None
            data_lag_note = "Azure SDK not installed"

        except Exception as e:
            logger.warning(f"Could not fetch Azure cost data: {e}")
            total_spend = None
            recent_spend = None
            data_lag_note = f"Cost data unavailable: {e}"

        resource_name = os.getenv("ANTHROPIC_FOUNDRY_RESOURCE") or "unknown-resource"  # noqa: env-ok

        return CostInfo(
            provider_name="Azure AI Foundry",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={
                "resource": resource_name,
                "base_url": os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),  # noqa: env-ok
            },
        )

    async def send_message(
        self,
        messages: List[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send message using Anthropic SDK via Azure Foundry.

        Azure Foundry uses the same Anthropic SDK with a different base URL.
        """
        import anthropic
        from anthropic.types import MessageParam, TextBlock

        # Get base URL from environment
        base_url = os.getenv("ANTHROPIC_FOUNDRY_BASE_URL")  # noqa: env-ok
        api_key = os.getenv("ANTHROPIC_FOUNDRY_API_KEY")  # noqa: env-ok

        client = anthropic.Anthropic(
            base_url=base_url,
            api_key=api_key or "placeholder",  # Azure Foundry may use Entra ID auth
        )

        # Use configured model or default
        effective_model = (
            model
            or os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")  # noqa: env-ok
            or "claude-sonnet-4-5"
        )

        # Convert to MessageParam type
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

        # Extract text from response
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
        Send message with tool definitions using Anthropic SDK via Azure Foundry.

        Returns full response including stop_reason and content blocks.
        """
        import anthropic
        from anthropic.types import ToolParam, ToolUseBlock

        # Get base URL from environment
        base_url = os.getenv("ANTHROPIC_FOUNDRY_BASE_URL")  # noqa: env-ok
        api_key = os.getenv("ANTHROPIC_FOUNDRY_API_KEY")  # noqa: env-ok

        client = anthropic.Anthropic(
            base_url=base_url,
            api_key=api_key or "placeholder",  # Azure Foundry may use Entra ID auth
        )

        # Use configured model or default
        effective_model = (
            model
            or os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")  # noqa: env-ok
            or "claude-sonnet-4-5"
        )

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

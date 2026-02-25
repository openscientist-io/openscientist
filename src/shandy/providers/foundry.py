"""
Azure AI Foundry (Microsoft Foundry) provider implementation.

Uses Azure AI Foundry for model access. Supports both API key and Entra ID authentication.
Cost tracking via Azure Cost Management API.
"""

import logging
import os
from datetime import UTC, datetime
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


class FoundryProvider(BaseProvider):
    """Azure AI Foundry provider."""

    @property
    def name(self) -> str:
        return "Azure AI Foundry"

    def _validate_required_config(self) -> list[str]:
        """Check required Foundry configuration."""
        errors = []
        settings = get_settings()

        # Resource name or base URL is required
        has_resource = settings.provider.anthropic_foundry_resource
        has_base_url = settings.provider.anthropic_foundry_base_url

        if not (has_resource or has_base_url):
            errors.append(
                "Azure Foundry resource not configured. Set either "
                "ANTHROPIC_FOUNDRY_RESOURCE (resource name) or "
                "ANTHROPIC_FOUNDRY_BASE_URL (full endpoint URL)"
            )

        # Check authentication - either API key or Azure credentials
        # If API key is not set, we assume Azure default credential chain is available
        has_api_key = settings.provider.anthropic_foundry_api_key
        if not has_api_key:
            logger.info(
                "ANTHROPIC_FOUNDRY_API_KEY not set. "
                "Will use Azure default credential chain (Entra ID authentication)"
            )

        return errors

    def _validate_optional_config(self) -> list[str]:
        """Check optional Foundry configuration."""
        warnings = []
        settings = get_settings()

        # Model deployment names (optional - Claude Code has defaults)
        if not settings.provider.anthropic_default_sonnet_model:
            warnings.append(
                "ANTHROPIC_DEFAULT_SONNET_MODEL not set "
                "(will use default deployment name 'claude-sonnet-4-5')"
            )

        if not settings.provider.anthropic_default_haiku_model:
            warnings.append(
                "ANTHROPIC_DEFAULT_HAIKU_MODEL not set "
                "(will use default deployment name 'claude-haiku-4-5')"
            )

        if not settings.provider.anthropic_default_opus_model:
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
        os.environ["CLAUDE_CODE_USE_FOUNDRY"] = "1"  # env-ok

        # Note: Claude Code 2.1.42+ will construct the base URL from ANTHROPIC_FOUNDRY_RESOURCE
        # automatically, so we don't need to set ANTHROPIC_FOUNDRY_BASE_URL here.
        # In fact, setting both causes an error: "baseURL and resource are mutually exclusive"

        # Unset conflicting provider routing vars
        clear_provider_mode_flags(logger, active_flag="CLAUDE_CODE_USE_FOUNDRY")
        clear_env_vars(logger, VERTEX_PROVIDER_ENV_VARS)
        clear_env_vars(logger, ("AWS_BEARER_TOKEN_BEDROCK",))

        # Unset direct Anthropic API key to avoid conflicts
        clear_env_vars(logger, ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))

        # Unset empty vars that interfere with auth
        # This happens when docker-compose passes VAR=${VAR} and it's unset
        empty_vars_to_clear = [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "AWS_PROFILE",
            "AWS_SESSION_TOKEN",
        ]
        clear_empty_env_vars(logger, empty_vars_to_clear)

        settings = get_settings()
        auth_method = "API key" if settings.provider.anthropic_foundry_api_key else "Entra ID"
        logger.info(f"Azure Foundry provider initialized (using {auth_method} authentication)")

    @staticmethod
    def _build_base_url_from_resource(resource: str) -> str:
        """Build Azure Foundry Anthropic endpoint URL from resource name."""
        return f"https://{resource}.services.ai.azure.com/api/anthropic"

    def _resolve_base_url(self) -> str:
        """Resolve Foundry base URL from explicit URL or resource name."""
        settings = get_settings()
        if settings.provider.anthropic_foundry_base_url:
            return settings.provider.anthropic_foundry_base_url
        if settings.provider.anthropic_foundry_resource:
            return self._build_base_url_from_resource(settings.provider.anthropic_foundry_resource)
        raise ValueError(
            "Azure Foundry endpoint not configured. Set ANTHROPIC_FOUNDRY_BASE_URL "
            "or ANTHROPIC_FOUNDRY_RESOURCE."
        )

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
        now = datetime.now(UTC)

        # Azure Cost Management API requires authentication and proper permissions
        # For now, return unavailable status with instructions
        # Check if Azure SDK is available using find_spec (no unused import)
        import importlib.util

        if importlib.util.find_spec("azure.identity") is None:
            logger.warning(
                "Azure SDK not installed. Cannot fetch cost data. "
                "Install with: pip install azure-identity azure-mgmt-costmanagement"
            )
            total_spend = None
            recent_spend = None
            data_lag_note = "Azure SDK not installed"
        else:
            # Azure SDK is available but cost tracking not yet implemented
            # Full implementation would:
            # 1. Get subscription_id from AZURE_SUBSCRIPTION_ID
            # 2. Initialize: credential = DefaultAzureCredential()
            # 3. Create client: cost_client = CostManagementClient(credential, subscription_id)
            # 4. Calculate time windows based on lookback_hours
            # 5. Query cost management API with proper filters for Foundry resource
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

        settings = get_settings()
        resource_name = settings.provider.anthropic_foundry_resource or "unknown-resource"

        return CostInfo(
            provider_name="Azure AI Foundry",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={
                "resource": resource_name,
                "base_url": settings.provider.anthropic_foundry_base_url,
            },
        )

    async def send_message(
        self,
        messages: list[dict[str, str]],
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

        settings = get_settings()
        base_url = self._resolve_base_url()
        api_key = settings.provider.anthropic_foundry_api_key

        client = anthropic.Anthropic(
            base_url=base_url,
            api_key=api_key or "placeholder",  # Azure Foundry may use Entra ID auth
        )

        # Use configured model or default
        effective_model = (
            model or settings.provider.anthropic_default_sonnet_model or "claude-sonnet-4-5"
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

        settings = get_settings()
        base_url = self._resolve_base_url()
        api_key = settings.provider.anthropic_foundry_api_key

        client = anthropic.Anthropic(
            base_url=base_url,
            api_key=api_key or "placeholder",  # Azure Foundry may use Entra ID auth
        )

        # Use configured model or default
        effective_model = (
            model or settings.provider.anthropic_default_sonnet_model or "claude-sonnet-4-5"
        )

        # Convert tools to ToolParam format
        tool_params: list[ToolParam] = build_tool_params(tools)  # type: ignore[assignment]

        # Use block format for system prompt with cache_control
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

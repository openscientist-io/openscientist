"""
Azure AI Foundry (Microsoft Foundry) provider implementation.

Uses Azure AI Foundry for model access. Supports both API key and Entra ID authentication.
Cost tracking via Azure Cost Management API.
"""

import logging
import os
from datetime import datetime, timezone
from typing import List

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
        has_resource = os.getenv("ANTHROPIC_FOUNDRY_RESOURCE")
        has_base_url = os.getenv("ANTHROPIC_FOUNDRY_BASE_URL")

        if not (has_resource or has_base_url):
            errors.append(
                "Azure Foundry resource not configured. Set either "
                "ANTHROPIC_FOUNDRY_RESOURCE (resource name) or "
                "ANTHROPIC_FOUNDRY_BASE_URL (full endpoint URL)"
            )

        # Check authentication - either API key or Azure credentials
        # If API key is not set, we assume Azure default credential chain is available
        has_api_key = os.getenv("ANTHROPIC_FOUNDRY_API_KEY")
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
        if not os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"):
            warnings.append(
                "ANTHROPIC_DEFAULT_SONNET_MODEL not set "
                "(will use default deployment name 'claude-sonnet-4-5')"
            )

        if not os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            warnings.append(
                "ANTHROPIC_DEFAULT_HAIKU_MODEL not set "
                "(will use default deployment name 'claude-haiku-4-5')"
            )

        if not os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"):
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
        os.environ["CLAUDE_CODE_USE_FOUNDRY"] = "1"

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
            if var in os.environ:
                logger.debug(f"Removing conflicting {var}")
                del os.environ[var]

        # Unset Bedrock vars to avoid conflicts
        bedrock_vars = [
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_BEARER_TOKEN_BEDROCK",
        ]
        for var in bedrock_vars:
            if var in os.environ:
                logger.debug(f"Removing conflicting {var}")
                del os.environ[var]

        # Unset direct Anthropic API key to avoid conflicts
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

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
            val = os.environ.get(var)
            if val == "":
                os.environ.pop(var, None)
                logger.debug(f"Unset empty {var}")

        auth_method = "API key" if os.getenv("ANTHROPIC_FOUNDRY_API_KEY") else "Entra ID"
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
        # Full implementation would use Azure SDK cost management client:
        #   from azure.identity import DefaultAzureCredential
        #   from azure.mgmt.costmanagement import CostManagementClient
        #   credential = DefaultAzureCredential()
        #   cost_client = CostManagementClient(credential, subscription_id)
        #   # Then query cost management API with proper filters for Foundry resource

        subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        if subscription_id:
            logger.info(f"Azure subscription ID configured: {subscription_id[:8]}...")

        logger.warning(
            "Azure Cost Management API integration not fully implemented. Cost data unavailable."
        )
        total_spend = None
        recent_spend = None
        data_lag_note = (
            "Azure cost tracking not yet implemented. View costs in Azure Portal > Cost Management"
        )

        resource_name = os.getenv("ANTHROPIC_FOUNDRY_RESOURCE") or "unknown-resource"

        return CostInfo(
            provider_name="Azure AI Foundry",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={
                "resource": resource_name,
                "base_url": os.getenv("ANTHROPIC_FOUNDRY_BASE_URL"),
            },
        )

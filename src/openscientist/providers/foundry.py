"""
Azure AI Foundry (Microsoft Foundry) provider implementation.

Uses Azure AI Foundry for model access. Supports both API key and Entra ID authentication.
Cost tracking via Azure Cost Management API.
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from openscientist.providers.base import ClaudeCompatible, CostInfo, LlmUpstream
from openscientist.settings import ProviderSettings, get_settings

from ._env_cleanup import (
    VERTEX_PROVIDER_ENV_VARS,
    clear_empty_env_vars,
    clear_env_vars,
    clear_provider_mode_flags,
)

logger = logging.getLogger(__name__)


def _query_azure_cost_usd(
    client: Any,
    scope: str,
    start: datetime,
    end: datetime,
) -> float:
    """
    Query Azure Cost Management for total cost between start and end.

    Returns the aggregated cost as a float. The currency is the subscription's
    billing currency (typically USD for US subscriptions).
    """
    from azure.mgmt.costmanagement.models import (  # noqa: PLC0415
        QueryAggregation,
        QueryDataset,
        QueryDefinition,
        QueryTimePeriod,
    )

    params = QueryDefinition(
        type="Usage",
        timeframe="Custom",
        time_period=QueryTimePeriod(from_property=start, to=end),
        dataset=QueryDataset(
            granularity="None",
            aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
        ),
    )
    result = client.query.usage(scope, params)
    if result is None or not result.rows:
        return 0.0
    return float(result.rows[0][0])


class FoundryProvider(ClaudeCompatible):
    """Azure AI Foundry provider."""

    @property
    def id(self) -> str:
        return "foundry"

    display_name = "Azure AI Foundry"

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        errors = []
        if not (provider.anthropic_foundry_resource or provider.anthropic_foundry_base_url):
            errors.append(
                "Azure Foundry resource not configured. Set either "
                "ANTHROPIC_FOUNDRY_RESOURCE (resource name) or "
                "ANTHROPIC_FOUNDRY_BASE_URL (full endpoint URL)"
            )
        if not provider.anthropic_foundry_api_key:
            try:
                import azure.identity  # noqa: F401, PLC0415
            except ImportError:
                errors.append(
                    "ANTHROPIC_FOUNDRY_API_KEY not set and azure-identity is not installed. "
                    "Either set ANTHROPIC_FOUNDRY_API_KEY or install: pip install azure-identity"
                )
            else:
                logger.info(
                    "ANTHROPIC_FOUNDRY_API_KEY not set. "
                    "Will use Azure default credential chain (Entra ID authentication)"
                )
        return errors

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        env = {"CLAUDE_CODE_USE_FOUNDRY": "1"}
        # Claude Code treats resource and base_url as mutually exclusive.
        if provider.anthropic_foundry_resource:
            env["ANTHROPIC_FOUNDRY_RESOURCE"] = provider.anthropic_foundry_resource
        elif provider.anthropic_foundry_base_url:
            env["ANTHROPIC_FOUNDRY_BASE_URL"] = provider.anthropic_foundry_base_url
        if provider.anthropic_foundry_api_key:
            env["ANTHROPIC_FOUNDRY_API_KEY"] = provider.anthropic_foundry_api_key
        return env

    def claude_sdk_env(self) -> dict[str, str]:
        """Foundry routing/auth env for the claude-agent-sdk CLI (its container env)."""
        return type(self).container_env(get_settings().provider)

    def llm_upstream(self) -> LlmUpstream | None:
        # Foundry authenticates with x-api-key (API key or minted Entra ID token).
        p = get_settings().provider
        if not (p.anthropic_foundry_base_url or p.anthropic_foundry_resource):
            return None
        return LlmUpstream(self._resolve_base_url(), {"x-api-key": self._resolve_api_key()})

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        p = get_settings().provider
        if not (p.anthropic_foundry_base_url or p.anthropic_foundry_resource):
            return {}
        return {
            "ANTHROPIC_FOUNDRY_BASE_URL": proxy_base_url,
            "ANTHROPIC_FOUNDRY_API_KEY": placeholder,
        }

    def proxied_container_env(
        self,
        *,
        proxy_base_url: str,
        placeholder: str,
        gcp_credentials_container_path: str | None = None,
    ) -> dict[str, str]:
        # Claude CLI foundry mode rejects resource and base_url together, so when
        # routing through the proxy the base_url wins and the resource is dropped.
        env = super().proxied_container_env(
            proxy_base_url=proxy_base_url,
            placeholder=placeholder,
            gcp_credentials_container_path=gcp_credentials_container_path,
        )
        if env.get("ANTHROPIC_FOUNDRY_BASE_URL") == proxy_base_url:
            env.pop("ANTHROPIC_FOUNDRY_RESOURCE", None)
        return env

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Route omp at Foundry.

        This is the case that failed in review. omp keys Foundry mode off
        ``CLAUDE_CODE_USE_FOUNDRY`` and then reads ``FOUNDRY_BASE_URL``, not the
        ``ANTHROPIC_FOUNDRY_BASE_URL`` that Claude Code and our container env
        use. With the name unset omp fell through its chain to
        ``https://api.anthropic.com``, which the air-gap firewall drops, so every
        turn hung until it timed out. Foundry mode sends the credential as
        ``Authorization: Bearer``, which the proxy already accepts, so the
        placeholder needs no special handling.
        """
        p = get_settings().provider
        if not (p.anthropic_foundry_base_url or p.anthropic_foundry_resource):
            return {}
        return {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "FOUNDRY_BASE_URL": proxy or self._resolve_base_url(),
        }

    def claude_model_name(self) -> str:
        """Model name for ClaudeAgentOptions.model."""
        return get_settings().provider.model or "claude-sonnet-4-5"

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
        logger.info("Azure Foundry provider initialized (using %s authentication)", auth_method)

    @staticmethod
    def _build_base_url_from_resource(resource: str) -> str:
        """Build Azure Foundry Anthropic endpoint URL from resource name."""
        return f"https://{resource}.services.ai.azure.com/anthropic"

    def _resolve_api_key(self) -> str:
        """Return bearer token for Anthropic client.

        Uses the configured API key if present; otherwise fetches a short-lived
        Entra ID token via Azure's default credential chain.
        """
        settings = get_settings()
        api_key = settings.provider.anthropic_foundry_api_key
        if api_key:
            return api_key
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415

        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        return token.token

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

        Requires AZURE_SUBSCRIPTION_ID to be set. Optionally AZURE_RESOURCE_GROUP
        scopes the query to a specific resource group (recommended to avoid pulling
        all subscription costs).

        Uses DefaultAzureCredential for authentication (Entra ID / managed identity).
        Cost data typically has a 1–3 day lag in Azure.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with Foundry spend data
        """
        now = datetime.now(UTC)
        settings = get_settings()
        subscription_id = settings.provider.azure_subscription_id

        if not subscription_id:
            resource_name = settings.provider.anthropic_foundry_resource or "Azure AI Foundry"
            return CostInfo(
                provider_name=f"Azure AI Foundry ({resource_name})",
                total_spend_usd=None,
                recent_spend_usd=None,
                recent_period_hours=lookback_hours,
                last_updated=now,
                data_lag_note=(
                    "Set AZURE_SUBSCRIPTION_ID to enable Azure Cost Management tracking"
                ),
                metadata={"resource": resource_name},
            )

        try:
            from azure.mgmt.costmanagement import CostManagementClient  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "Azure Cost Management SDK not installed. "
                "Run: pip install azure-identity azure-mgmt-costmanagement"
            )
            return CostInfo(
                provider_name="Azure AI Foundry",
                total_spend_usd=None,
                recent_spend_usd=None,
                recent_period_hours=lookback_hours,
                last_updated=now,
                data_lag_note=(
                    "Install azure-mgmt-costmanagement to enable billing: "
                    "pip install azure-mgmt-costmanagement"
                ),
            )

        tenant_id = settings.provider.azure_tenant_id
        client_id = settings.provider.azure_client_id
        client_secret = settings.provider.azure_client_secret

        if not (tenant_id and client_id and client_secret):
            return CostInfo(
                provider_name="Azure AI Foundry",
                total_spend_usd=None,
                recent_spend_usd=None,
                recent_period_hours=lookback_hours,
                last_updated=now,
                data_lag_note=(
                    "Set AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET "
                    "to enable Azure Cost Management tracking."
                ),
            )

        resource_group = settings.provider.azure_resource_group
        if resource_group:
            scope = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        else:
            scope = f"/subscriptions/{subscription_id}"

        try:
            from azure.identity import ClientSecretCredential  # noqa: PLC0415

            credential = ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
            )
            client = CostManagementClient(credential)

            # Total spend: current calendar month (month-to-date)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            total_spend = _query_azure_cost_usd(client, scope, month_start, now)

            # Recent spend: last lookback_hours
            recent_start = now - timedelta(hours=lookback_hours)
            recent_spend = _query_azure_cost_usd(client, scope, recent_start, now)

            resource_name = settings.provider.anthropic_foundry_resource or "Azure AI Foundry"
            return CostInfo(
                provider_name=f"Azure AI Foundry ({resource_name})",
                total_spend_usd=total_spend,
                recent_spend_usd=recent_spend,
                recent_period_hours=lookback_hours,
                last_updated=now,
                data_lag_note="Azure billing data may have 1–3 day lag",
                metadata={
                    "scope": scope,
                    "resource": settings.provider.anthropic_foundry_resource,
                },
            )

        except Exception as e:
            logger.warning("Azure Cost Management query failed: %s", e)
            return CostInfo(
                provider_name="Azure AI Foundry",
                total_spend_usd=None,
                recent_spend_usd=None,
                recent_period_hours=lookback_hours,
                last_updated=now,
                data_lag_note=(
                    "Azure Cost Management unavailable. "
                    "Check Azure credentials. See server logs for details."
                ),
            )

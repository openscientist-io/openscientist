"""
Google Cloud Vertex AI provider implementation.

Uses Vertex AI for model access and GCP Billing API for cost tracking.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from shandy.exceptions import ProviderError
from shandy.providers.base import BaseProvider, CostInfo
from shandy.settings import get_settings

logger = logging.getLogger(__name__)


class VertexProvider(BaseProvider):
    """Google Cloud Vertex AI provider."""

    @property
    def name(self) -> str:
        return "Vertex AI"

    def _validate_required_config(self) -> list[str]:
        """Check required Vertex AI configuration."""
        errors = []
        settings = get_settings()

        if not settings.provider.anthropic_vertex_project_id:
            errors.append("ANTHROPIC_VERTEX_PROJECT_ID not set (GCP project ID)")

        creds_env = settings.provider.google_application_credentials
        if not creds_env:
            errors.append("GOOGLE_APPLICATION_CREDENTIALS not set (path to service account JSON)")
        else:
            # Check if file exists
            creds_path = os.path.expanduser(creds_env)
            if not os.path.exists(creds_path):
                errors.append(f"GOOGLE_APPLICATION_CREDENTIALS file not found: {creds_path}")

        if not settings.provider.gcp_billing_account_id:
            errors.append("GCP_BILLING_ACCOUNT_ID not set (needed for cost tracking)")

        if not settings.provider.cloud_ml_region:
            errors.append("CLOUD_ML_REGION not set (e.g., us-east5)")

        return errors

    def _validate_optional_config(self) -> list[str]:
        """Check optional Vertex AI configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.anthropic_model:
            warnings.append("ANTHROPIC_MODEL not set (will use claude-sonnet-4-5@20250929)")

        if not settings.provider.vertex_region_claude_4_5_sonnet:
            warnings.append("VERTEX_REGION_CLAUDE_4_5_SONNET not set (may cause region issues)")

        if not settings.provider.vertex_region_claude_4_5_haiku:
            warnings.append("VERTEX_REGION_CLAUDE_4_5_HAIKU not set (may cause region issues)")

        return warnings

    def setup_environment(self) -> None:
        """Vertex AI environment should be configured via .env and docker-compose.yml."""
        # Unset conflicting provider vars
        os.environ.pop("CLAUDE_CODE_USE_BEDROCK", None)  # env-ok
        os.environ.pop("ANTHROPIC_API_KEY", None)  # env-ok
        logger.info("Vertex AI provider initialized (configuration from environment)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get Vertex AI cost information from Cloud Billing API.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with Vertex AI spend data

        Note:
            Requires BigQuery billing export to be enabled in GCP Console.
            Data has 1-6 hour lag due to GCP billing pipeline.
        """
        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account
        except ImportError:
            logger.error(
                "google-cloud-bigquery not installed. "
                "Install with: pip install google-cloud-bigquery"
            )
            raise

        # Load credentials
        settings = get_settings()
        creds_env = settings.provider.google_application_credentials
        if not creds_env:
            raise ProviderError("GOOGLE_APPLICATION_CREDENTIALS not set")
        creds_path = os.path.expanduser(creds_env)
        credentials = service_account.Credentials.from_service_account_file(creds_path)

        project_id = settings.provider.anthropic_vertex_project_id
        billing_account = settings.provider.gcp_billing_account_id
        if not billing_account:
            raise ProviderError("GCP_BILLING_ACCOUNT_ID not set")

        # Initialize BigQuery client
        bq_client = bigquery.Client(credentials=credentials, project=project_id)

        # Calculate time windows
        now = datetime.now(timezone.utc)
        recent_start = now - timedelta(hours=lookback_hours)

        # BigQuery billing export table name
        # Format: billing_export.gcp_billing_export_v1_{billing_account_with_underscores}
        billing_table = (
            f"{project_id}.billing_export.gcp_billing_export_v1_{billing_account.replace('-', '_')}"
        )

        # Query for total spend (all time for Vertex AI in this project only)
        total_query = f"""
        SELECT SUM(cost) as total_cost
        FROM `{billing_table}`
        WHERE service.description = 'Vertex AI'
          AND project.id = '{project_id}'
        """

        # Query for recent spend (filter by project AND time window)
        recent_query = f"""
        SELECT SUM(cost) as recent_cost
        FROM `{billing_table}`
        WHERE service.description = 'Vertex AI'
          AND project.id = '{project_id}'
          AND usage_start_time >= TIMESTAMP('{recent_start.isoformat()}')
        """

        try:
            # Execute queries
            total_result = list(bq_client.query(total_query).result())
            total_spend = float(total_result[0].total_cost or 0)

            recent_result = list(bq_client.query(recent_query).result())
            recent_spend = float(recent_result[0].recent_cost or 0)

            # Estimate data lag (GCP billing typically has 1-6 hour delay)
            # Assume ~3 hour average lag
            lag_time = now - timedelta(hours=3)
            data_lag_note = f"Data current as of ~{lag_time.strftime('%I:%M %p %Z')}"

        except Exception as e:  # noqa: BLE001 — google-cloud exceptions are dynamic
            logger.warning("Could not fetch Vertex AI billing data: %s", e)
            logger.warning(
                "Ensure BigQuery billing export is enabled in GCP Console. "
                "See docs/VERTEX_SETUP.md for setup instructions."
            )
            # Return None if billing data unavailable (permissions, export not enabled, etc.)
            total_spend = None
            recent_spend = None
            data_lag_note = "Billing data unavailable (ensure BigQuery export is enabled)"

        return CostInfo(
            provider_name="Vertex AI",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={"project_id": project_id},
        )

    async def send_message(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send message using Vertex AI via Anthropic SDK.

        This bypasses the Claude Code CLI and its local pre-flight content
        filter, which can produce false positives on legitimate scientific content.
        """
        import anthropic
        from anthropic.types import MessageParam, TextBlock

        settings = get_settings()

        # These are validated as required in _validate_required_config
        project_id = settings.provider.anthropic_vertex_project_id
        region = settings.provider.cloud_ml_region
        if not project_id or not region:
            raise ValueError("Vertex AI project_id and region are required")

        client = anthropic.AnthropicVertex(
            project_id=project_id,
            region=region,
        )

        # Use configured model or default
        effective_model = model or settings.provider.anthropic_model or "claude-sonnet-4-5@20250929"

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
        Send message with tool definitions using Vertex AI via Anthropic SDK.

        Returns full response including stop_reason and content blocks.
        """
        import anthropic
        from anthropic.types import ToolParam, ToolUseBlock

        settings = get_settings()

        # These are validated as required in _validate_required_config
        project_id = settings.provider.anthropic_vertex_project_id
        region = settings.provider.cloud_ml_region
        if not project_id or not region:
            raise ValueError("Vertex AI project_id and region are required")

        client = anthropic.AnthropicVertex(
            project_id=project_id,
            region=region,
        )

        # Use configured model or default
        effective_model = model or settings.provider.anthropic_model or "claude-sonnet-4-5@20250929"

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

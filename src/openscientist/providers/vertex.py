"""
Google Cloud Vertex AI provider implementation.

Uses Vertex AI for model access and GCP Billing API for cost tracking.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

from openscientist.exceptions import ProviderError
from openscientist.providers.base import (
    AirgapEgress,
    AirgapPosture,
    ClaudeCompatible,
    CostInfo,
    env_from_pairs,
)
from openscientist.settings import ProviderSettings, get_settings

from ._env_cleanup import clear_env_vars, clear_provider_mode_flags

logger = logging.getLogger(__name__)


class VertexProvider(ClaudeCompatible):
    """Google Cloud Vertex AI provider."""

    @property
    def id(self) -> str:
        return "vertex"

    display_name = "Vertex AI"

    @classmethod
    def validate_model_format(cls, model: str | None) -> str | None:
        return cls.model_format_error(
            model, r"^claude-.+@\d{8}$", "a Vertex Anthropic model id ('claude-<name>@<YYYYMMDD>')"
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        errors = []
        if not provider.anthropic_vertex_project_id:
            errors.append("ANTHROPIC_VERTEX_PROJECT_ID not set (GCP project ID)")
        creds_env = provider.google_application_credentials
        if not creds_env:
            errors.append("GOOGLE_APPLICATION_CREDENTIALS not set (path to service account JSON)")
        elif not os.path.exists(os.path.expanduser(creds_env)):
            errors.append(
                f"GOOGLE_APPLICATION_CREDENTIALS file not found: {os.path.expanduser(creds_env)}"
            )
        if not provider.gcp_billing_account_id:
            errors.append("GCP_BILLING_ACCOUNT_ID not set (needed for cost tracking)")
        if not provider.cloud_ml_region:
            errors.append("CLOUD_ML_REGION not set (e.g., us-east5)")
        return errors

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        env = {"CLAUDE_CODE_USE_VERTEX": "1"}
        env.update(
            env_from_pairs(
                [
                    ("ANTHROPIC_VERTEX_PROJECT_ID", provider.anthropic_vertex_project_id),
                    ("GCP_BILLING_ACCOUNT_ID", provider.gcp_billing_account_id),
                    ("CLOUD_ML_REGION", provider.cloud_ml_region),
                    ("VERTEX_REGION_CLAUDE_4_5_SONNET", provider.vertex_region_claude_4_5_sonnet),
                    ("VERTEX_REGION_CLAUDE_4_5_HAIKU", provider.vertex_region_claude_4_5_haiku),
                ]
            )
        )
        if gcp_credentials_container_path:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = gcp_credentials_container_path
        return env

    def claude_sdk_env(self) -> dict[str, str]:
        """Vertex routing/auth env for the claude-agent-sdk CLI (its container env)."""
        provider = get_settings().provider
        return type(self).container_env(
            provider, gcp_credentials_container_path=provider.google_application_credentials
        )

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Route omp at Vertex.

        Vertex signs its own requests with application-default credentials, so it
        is never proxied and ``airgap_egress`` reports DIRECT. omp reads the
        standard Google names rather than the Claude Code ones we publish, so the
        project and region have to be translated or omp falls back to the public
        Anthropic endpoint.
        """
        p = get_settings().provider
        return env_from_pairs(
            [
                ("GOOGLE_CLOUD_PROJECT", p.anthropic_vertex_project_id),
                ("GOOGLE_CLOUD_LOCATION", p.cloud_ml_region),
                ("GOOGLE_APPLICATION_CREDENTIALS", p.google_application_credentials),
            ]
        )

    def claude_model_name(self) -> str:
        """Model name for ClaudeAgentOptions.model."""
        return get_settings().provider.model or "claude-sonnet-4-5@20250929"

    def airgap_egress(self) -> AirgapPosture:
        region = get_settings().provider.cloud_ml_region
        return AirgapPosture(
            AirgapEgress.DIRECT,
            direct_endpoints=(
                (f"{region}-aiplatform.googleapis.com", 443),
                ("oauth2.googleapis.com", 443),
            ),
        )

    def _validate_optional_config(self) -> list[str]:
        """Check optional Vertex AI configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.model:
            warnings.append("OPENSCIENTIST_MODEL not set (will use claude-sonnet-4-5@20250929)")

        if not settings.provider.vertex_region_claude_4_5_sonnet:
            warnings.append("VERTEX_REGION_CLAUDE_4_5_SONNET not set (may cause region issues)")

        if not settings.provider.vertex_region_claude_4_5_haiku:
            warnings.append("VERTEX_REGION_CLAUDE_4_5_HAIKU not set (may cause region issues)")

        return warnings

    def setup_environment(self) -> None:
        """Vertex AI environment should be configured via .env and docker-compose.yml."""
        # Keep existing Vertex mode flag (if configured) and clear conflicting ones.
        clear_provider_mode_flags(logger, active_flag="CLAUDE_CODE_USE_VERTEX")
        clear_env_vars(logger, ("ANTHROPIC_API_KEY",))
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
        credentials = service_account.Credentials.from_service_account_file(creds_path)  # type: ignore[no-untyped-call]

        project_id = settings.provider.anthropic_vertex_project_id
        billing_account = settings.provider.gcp_billing_account_id
        if not billing_account:
            raise ProviderError("GCP_BILLING_ACCOUNT_ID not set")

        # Initialize BigQuery client
        bq_client = bigquery.Client(credentials=credentials, project=project_id)

        # Calculate time windows
        now = datetime.now(UTC)
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

        except Exception as e:
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

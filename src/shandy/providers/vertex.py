"""
Google Cloud Vertex AI provider implementation.

Uses Vertex AI for model access and GCP Billing API for cost tracking.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

from .base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class VertexProvider(BaseProvider):
    """Google Cloud Vertex AI provider."""

    @property
    def name(self) -> str:
        return "Vertex AI"

    def _validate_required_config(self) -> List[str]:
        """Check required Vertex AI configuration."""
        errors = []

        if not os.getenv("ANTHROPIC_VERTEX_PROJECT_ID"):
            errors.append("ANTHROPIC_VERTEX_PROJECT_ID not set (GCP project ID)")

        if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            errors.append(
                "GOOGLE_APPLICATION_CREDENTIALS not set (path to service account JSON)"
            )
        else:
            # Check if file exists
            creds_path = os.path.expanduser(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
            if not os.path.exists(creds_path):
                errors.append(
                    f"GOOGLE_APPLICATION_CREDENTIALS file not found: {creds_path}"
                )

        if not os.getenv("GCP_BILLING_ACCOUNT_ID"):
            errors.append("GCP_BILLING_ACCOUNT_ID not set (needed for cost tracking)")

        if not os.getenv("CLOUD_ML_REGION"):
            errors.append("CLOUD_ML_REGION not set (e.g., us-east5)")

        return errors

    def _validate_optional_config(self) -> List[str]:
        """Check optional Vertex AI configuration."""
        warnings = []

        if not os.getenv("ANTHROPIC_MODEL"):
            warnings.append(
                "ANTHROPIC_MODEL not set (will use claude-sonnet-4-5@20250929)"
            )

        if not os.getenv("VERTEX_REGION_CLAUDE_4_5_SONNET"):
            warnings.append(
                "VERTEX_REGION_CLAUDE_4_5_SONNET not set (may cause region issues)"
            )

        if not os.getenv("VERTEX_REGION_CLAUDE_4_5_HAIKU"):
            warnings.append(
                "VERTEX_REGION_CLAUDE_4_5_HAIKU not set (may cause region issues)"
            )

        return warnings

    def setup_environment(self) -> None:
        """Set up Vertex AI environment for Claude CLI."""
        # Enable Vertex AI mode
        os.environ["CLAUDE_CODE_USE_VERTEX"] = "1"

        # Clear CBORG settings if present
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

        # Set defaults if not present
        if not os.getenv("ANTHROPIC_MODEL"):
            os.environ["ANTHROPIC_MODEL"] = "claude-sonnet-4-5@20250929"

        if not os.getenv("ANTHROPIC_SMALL_FAST_MODEL"):
            os.environ["ANTHROPIC_SMALL_FAST_MODEL"] = "claude-haiku-4-5@20251001"

        logger.info("Vertex AI provider environment configured")

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
        creds_path = os.path.expanduser(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
        credentials = service_account.Credentials.from_service_account_file(creds_path)

        project_id = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
        billing_account = os.getenv("GCP_BILLING_ACCOUNT_ID")

        # Initialize BigQuery client
        bq_client = bigquery.Client(credentials=credentials, project=project_id)

        # Calculate time windows
        now = datetime.now(timezone.utc)
        recent_start = now - timedelta(hours=lookback_hours)

        # BigQuery billing export table name
        # Format: billing_export.gcp_billing_export_v1_{billing_account_with_underscores}
        billing_table = f"{project_id}.billing_export.gcp_billing_export_v1_{billing_account.replace('-', '_')}"

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
            logger.warning(f"Could not fetch Vertex AI billing data: {e}")
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
            metadata={"project_id": project_id}
        )

"""
CBORG provider implementation.

Uses CBORG API for model access and cost tracking.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

import requests

from .base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class CborgProvider(BaseProvider):
    """CBORG API provider (current implementation)."""

    @property
    def name(self) -> str:
        return "CBORG"

    def _validate_required_config(self) -> List[str]:
        """Check required CBORG configuration."""
        errors = []

        if not os.getenv("ANTHROPIC_AUTH_TOKEN"):
            errors.append("ANTHROPIC_AUTH_TOKEN not set (required for CBORG)")

        if not os.getenv("ANTHROPIC_BASE_URL"):
            errors.append("ANTHROPIC_BASE_URL not set (should be https://api.cborg.lbl.gov)")

        return errors

    def _validate_optional_config(self) -> List[str]:
        """Check optional CBORG configuration."""
        warnings = []

        if not os.getenv("ANTHROPIC_MODEL"):
            warnings.append("ANTHROPIC_MODEL not set (will use Claude CLI default)")

        return warnings

    def setup_environment(self) -> None:
        """Set up CBORG environment for Claude CLI."""
        # Disable Vertex AI if accidentally set
        os.environ.pop('CLAUDE_CODE_USE_VERTEX', None)
        os.environ.pop('ANTHROPIC_VERTEX_PROJECT_ID', None)
        os.environ.pop('GOOGLE_APPLICATION_CREDENTIALS', None)

        # Set ANTHROPIC_API_KEY from ANTHROPIC_AUTH_TOKEN
        # (Claude CLI uses ANTHROPIC_API_KEY, SHANDY uses ANTHROPIC_AUTH_TOKEN)
        if not os.getenv("ANTHROPIC_API_KEY") and os.getenv("ANTHROPIC_AUTH_TOKEN"):
            os.environ["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_AUTH_TOKEN")

        logger.info("CBORG provider environment configured")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get CBORG cost information.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with CBORG spend data
        """
        token = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if not token:
            raise ValueError("ANTHROPIC_AUTH_TOKEN not set")

        # Get total spend and budget from /key/info
        try:
            info_response = requests.get(
                "https://api.cborg.lbl.gov/key/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            info_response.raise_for_status()
            info = info_response.json()["info"]

            total_spend = info["spend"]
            max_budget = info.get("max_budget")
            key_expires = info.get("expires")

        except Exception as e:
            logger.error(f"Failed to fetch CBORG /key/info: {e}")
            raise

        # Get recent spend from /user/daily/activity
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=lookback_hours)

            activity_response = requests.get(
                "https://api.cborg.lbl.gov/user/daily/activity",
                params={
                    "start_date": start_time.isoformat(),
                    "end_date": end_time.isoformat(),
                    "page": 1,
                    "page_size": 1000
                },
                headers={"x-litellm-api-key": token},
                timeout=10
            )
            activity_response.raise_for_status()

            # Sum up costs from activity records
            activity_data = activity_response.json().get("data", [])
            recent_spend = sum(
                record.get("spend", 0)
                for record in activity_data
            )

        except Exception as e:
            logger.warning(f"Failed to fetch CBORG activity data: {e}")
            # Fall back to 0 if activity endpoint fails
            recent_spend = 0.0

        # Calculate budget remaining
        budget_remaining = None
        if max_budget is not None:
            budget_remaining = max_budget - total_spend

        return CostInfo(
            provider_name="CBORG",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            budget_limit_usd=max_budget,
            budget_remaining_usd=budget_remaining,
            last_updated=datetime.now(timezone.utc),
            key_expires=key_expires
        )

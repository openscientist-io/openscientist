"""
AWS Bedrock provider implementation.

Uses AWS Bedrock for model access. Cost tracking via AWS Cost Explorer.
"""

import logging
import os
from datetime import datetime, timezone
from typing import List

from shandy.providers.base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class BedrockProvider(BaseProvider):
    """AWS Bedrock provider."""

    @property
    def name(self) -> str:
        return "AWS Bedrock"

    def _validate_required_config(self) -> List[str]:
        """Check required Bedrock configuration."""
        errors = []

        if not os.getenv("AWS_REGION"):
            errors.append("AWS_REGION not set (e.g., us-east-1)")

        # Check for at least one auth method
        has_access_key = os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")
        has_profile = os.getenv("AWS_PROFILE")
        has_bearer_token = os.getenv("AWS_BEARER_TOKEN_BEDROCK")

        if not (has_access_key or has_profile or has_bearer_token):
            errors.append(
                "AWS credentials not configured. Set one of: "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, or AWS_BEARER_TOKEN_BEDROCK"
            )

        return errors

    def _validate_optional_config(self) -> List[str]:
        """Check optional Bedrock configuration."""
        warnings = []

        if not os.getenv("ANTHROPIC_MODEL"):
            warnings.append(
                "ANTHROPIC_MODEL not set (will use global.anthropic.claude-sonnet-4-5-20250929-v1:0)"
            )

        if not os.getenv("ANTHROPIC_SMALL_FAST_MODEL"):
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
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"

        # Unset Vertex-related vars to avoid conflicts
        os.environ.pop("CLAUDE_CODE_USE_VERTEX", None)
        os.environ.pop("ANTHROPIC_VERTEX_PROJECT_ID", None)
        os.environ.pop("VERTEX_REGION_CLAUDE_4_5_SONNET", None)
        os.environ.pop("VERTEX_REGION_CLAUDE_4_5_HAIKU", None)

        # Unset direct API key to avoid conflicts
        os.environ.pop("ANTHROPIC_API_KEY", None)

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
        now = datetime.now(timezone.utc)

        # AWS Cost Explorer requires ce:GetCostAndUsage permission
        # For now, return unavailable status with instructions
        # Full implementation would use boto3 cost explorer client
        try:
            import boto3
            from datetime import timedelta

            # Initialize Cost Explorer client
            ce_client = boto3.client("ce", region_name=os.getenv("AWS_REGION", "us-east-1"))

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
                    Filter={
                        "Dimensions": {
                            "Key": "SERVICE",
                            "Values": ["Amazon Bedrock"]
                        }
                    }
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

        return CostInfo(
            provider_name="AWS Bedrock",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={"region": os.getenv("AWS_REGION")}
        )

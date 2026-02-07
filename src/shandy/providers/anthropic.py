"""
Direct Anthropic API provider.

For users who want to use their own Anthropic API key.
"""

import logging
import os
from typing import List

from .base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseProvider):
    """
    Direct Anthropic API provider.

    Uses ANTHROPIC_API_KEY for authentication directly with Anthropic's API.
    This is for users who want to bring their own personal API key.
    """

    @property
    def name(self) -> str:
        return "Anthropic"

    def _validate_required_config(self) -> List[str]:
        """Check required Anthropic configuration."""
        errors = []

        if not os.getenv("ANTHROPIC_API_KEY"):
            errors.append(
                "ANTHROPIC_API_KEY not set. Get your API key from https://console.anthropic.com"
            )

        return errors

    def _validate_optional_config(self) -> List[str]:
        """Check optional configuration."""
        warnings = []

        if not os.getenv("ANTHROPIC_MODEL"):
            warnings.append("ANTHROPIC_MODEL not set (will use Claude CLI default)")

        return warnings

    def setup_environment(self) -> None:
        """Anthropic environment is configured via ANTHROPIC_API_KEY in .env."""
        # Unset Vertex-related vars to ensure Claude Code uses ANTHROPIC_API_KEY
        os.environ.pop("CLAUDE_CODE_USE_VERTEX", None)
        os.environ.pop("ANTHROPIC_VERTEX_PROJECT_ID", None)
        logger.info("Anthropic provider initialized (using ANTHROPIC_API_KEY)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get cost information.

        Note: Anthropic's API doesn't provide a usage/billing endpoint,
        so we return unknown for cost tracking.
        """
        return CostInfo(
            provider_name=self.name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
            data_lag_note="Cost tracking not available for direct Anthropic API",
        )

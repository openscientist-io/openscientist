"""
AWS Bedrock provider implementation (STUB - NOT YET IMPLEMENTED).

This provider is a placeholder for future AWS Bedrock support.
"""

import logging
from typing import List

from shandy.providers.base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class BedrockProvider(BaseProvider):
    """AWS Bedrock provider (NOT YET IMPLEMENTED)."""

    @property
    def name(self) -> str:
        return "AWS Bedrock"

    def _validate_required_config(self) -> List[str]:
        """Bedrock validation - not implemented."""
        return [
            "AWS Bedrock provider is not yet implemented.",
            "Please use 'cborg' or 'vertex' as CLAUDE_PROVIDER.",
        ]

    def setup_environment(self) -> None:
        """Bedrock environment setup - not implemented."""
        raise NotImplementedError(
            "AWS Bedrock provider coming soon. "
            "Use CLAUDE_PROVIDER=cborg or CLAUDE_PROVIDER=vertex instead."
        )

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Bedrock cost tracking - not implemented."""
        raise NotImplementedError(
            "AWS Bedrock provider coming soon. "
            "Use CLAUDE_PROVIDER=cborg or CLAUDE_PROVIDER=vertex instead."
        )

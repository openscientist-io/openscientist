"""
OpenAI Codex provider implementation (STUB - NOT YET IMPLEMENTED).

This provider is a placeholder for future OpenAI Codex support.
"""

import logging
from typing import List

from shandy.providers.base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class CodexProvider(BaseProvider):
    """OpenAI Codex provider (NOT YET IMPLEMENTED)."""

    @property
    def name(self) -> str:
        return "OpenAI Codex"

    def _validate_required_config(self) -> List[str]:
        """Codex validation - not implemented."""
        return [
            "OpenAI Codex provider is not yet implemented.",
            "Please use 'cborg', 'anthropic', or 'vertex' as CLAUDE_PROVIDER.",
        ]

    def setup_environment(self) -> None:
        """Codex environment setup - not implemented."""
        raise NotImplementedError(
            "OpenAI Codex provider coming soon. "
            "Use CLAUDE_PROVIDER=cborg or CLAUDE_PROVIDER=vertex instead."
        )

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Codex cost tracking - not implemented."""
        raise NotImplementedError(
            "OpenAI Codex provider coming soon. "
            "Use CLAUDE_PROVIDER=cborg or CLAUDE_PROVIDER=vertex instead."
        )

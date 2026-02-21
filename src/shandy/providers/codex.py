"""
OpenAI Codex provider implementation (STUB - NOT YET IMPLEMENTED).

This provider is a placeholder for future OpenAI Codex support.
"""

import logging
from typing import Any

from shandy.providers.base import BaseProvider, CostInfo

logger = logging.getLogger(__name__)


class CodexProvider(BaseProvider):
    """OpenAI Codex provider (NOT YET IMPLEMENTED)."""

    @property
    def name(self) -> str:
        return "OpenAI Codex"

    def _validate_required_config(self) -> list[str]:
        """Codex validation - not implemented."""
        return [
            "OpenAI Codex provider is not yet implemented.",
            "Please use 'anthropic', 'cborg', 'vertex', 'bedrock', or 'foundry' as CLAUDE_PROVIDER.",
        ]

    def setup_environment(self) -> None:
        """Codex environment setup - not implemented."""
        raise NotImplementedError(
            "OpenAI Codex provider coming soon. "
            "Use CLAUDE_PROVIDER=anthropic, vertex, bedrock, or foundry instead."
        )

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Codex cost tracking - not implemented."""
        raise NotImplementedError(
            "OpenAI Codex provider coming soon. "
            "Use CLAUDE_PROVIDER=anthropic, vertex, bedrock, or foundry instead."
        )

    async def send_message(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        raise NotImplementedError("OpenAI Codex provider is not yet implemented.")

    async def send_message_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        raise NotImplementedError("OpenAI Codex provider is not yet implemented.")

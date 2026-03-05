"""
Base provider interface and shared types.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from openscientist.exceptions import ProviderError
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class CostInfo:
    """Provider-agnostic cost information."""

    provider_name: str

    # Total project spending (all time)
    # None = unknown/unavailable (e.g., permissions error)
    total_spend_usd: float | None

    # Recent spending (configurable time window)
    # None = unknown/unavailable (e.g., permissions error)
    recent_spend_usd: float | None
    recent_period_hours: int  # e.g., 24 for "last 24h"

    # Budget tracking (optional - provider-specific)
    budget_limit_usd: float | None = None
    budget_remaining_usd: float | None = None

    # Data freshness
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))
    data_lag_note: str | None = None  # e.g., "Data current as of 6:35 AM ET"

    # Provider-specific extras
    key_expires: str | None = None  # CBORG only
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Abstract base class for model providers."""

    def __init__(self) -> None:
        """Initialize and validate provider configuration."""
        errors = self._validate_required_config()
        if errors:
            raise ValueError(
                f"{self.name} provider configuration errors:\n"
                + "\n".join(f"  - {err}" for err in errors)
            )

        warnings = self._validate_optional_config()
        if warnings:
            logger.warning(
                "%s provider configuration warnings:\n%s",
                self.name,
                "\n".join(f"  - {warn}" for warn in warnings),
            )

    @abstractmethod
    def _validate_required_config(self) -> list[str]:
        """
        Validate required configuration.

        Returns:
            List of error messages (empty if valid)
        """

    def _validate_optional_config(self) -> list[str]:
        """
        Validate optional configuration.

        Returns:
            List of warning messages (empty if valid)
        """
        return []  # Default: no optional config

    @abstractmethod
    def setup_environment(self) -> None:
        """Configure environment variables for Claude CLI."""

    @abstractmethod
    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get project spending information.

        Args:
            lookback_hours: Time window for recent_spend_usd

        Returns:
            CostInfo with total and recent spend
        """

    def check_budget_limits(self, lookback_hours: int = 24) -> dict[str, Any]:
        """
        Check if budget limits are exceeded.

        Returns:
            {
                "can_proceed": bool,
                "warnings": List[str],
                "errors": List[str]
            }
        """
        try:
            cost_info = self.get_cost_info(lookback_hours=lookback_hours)
        except (ProviderError, ValueError, OSError) as e:
            logger.error("Could not fetch cost info for budget check: %s", e)
            # If we can't check costs, allow job to proceed but warn
            return {
                "can_proceed": True,
                "warnings": [f"Could not check budget limits: {e}"],
                "errors": [],
            }

        warnings = []
        errors = []

        # If cost data is unavailable, warn but allow job to proceed
        if cost_info.total_spend_usd is None or cost_info.recent_spend_usd is None:
            warnings.append(
                f"Cost data unavailable for budget check. "
                f"Reason: {cost_info.data_lag_note or 'Unknown'}"
            )
        else:
            settings = get_settings()
            # Check total spend limit
            max_total = settings.budget.max_project_spend_total_usd
            if cost_info.total_spend_usd >= max_total:
                errors.append(
                    f"Total spend ${cost_info.total_spend_usd:.2f} exceeds limit ${max_total:.2f}"
                )

            # Check 24h spend limit (use settings for default, assumes 24h lookback)
            max_recent = settings.budget.max_project_spend_24h_usd
            if cost_info.recent_spend_usd >= max_recent:
                errors.append(
                    f"Last {lookback_hours}h spend ${cost_info.recent_spend_usd:.2f} "
                    f"exceeds limit ${max_recent:.2f}"
                )

            # Check warning threshold
            warn_recent = settings.budget.warn_project_spend_24h_usd
            if (
                cost_info.recent_spend_usd >= warn_recent
                and cost_info.recent_spend_usd < max_recent
            ):
                warnings.append(
                    f"Last {lookback_hours}h spend ${cost_info.recent_spend_usd:.2f} "
                    f"approaching limit (warning threshold: ${warn_recent:.2f})"
                )

        # Provider-specific budget (e.g., CBORG max_budget)
        if cost_info.budget_remaining_usd is not None:
            if cost_info.budget_remaining_usd <= 0:
                errors.append(
                    f"{self.name} budget exhausted (${cost_info.budget_limit_usd or 0:.2f} limit)"
                )
            elif cost_info.budget_remaining_usd < 10:
                warnings.append(
                    f"{self.name} budget low: ${cost_info.budget_remaining_usd:.2f} remaining"
                )

        return {"can_proceed": len(errors) == 0, "warnings": warnings, "errors": errors}

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging/display."""

    @abstractmethod
    async def send_message(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send a message using the provider's SDK and return response text.

        This method bypasses the Claude Code CLI and calls the API directly,
        avoiding the CLI's local pre-flight content filter which can produce
        false positives on legitimate scientific content.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            system: Optional system prompt
            model: Optional model override (uses provider default if not set)
            max_tokens: Maximum tokens in response (default 4096)

        Returns:
            The assistant's response text

        Raises:
            Exception: If the API call fails
        """

    @abstractmethod
    async def send_message_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """
        Send a message with tool definitions and return full response.

        This method supports the agentic loop pattern where the model can
        request tool calls, and the caller handles execution.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
                     Content can be a string or list of content blocks.
            tools: List of tool definitions in Anthropic format:
                   [{"name": str, "description": str, "input_schema": dict}, ...]
            system: Optional system prompt
            model: Optional model override (uses provider default if not set)
            max_tokens: Maximum tokens in response (default 4096)

        Returns:
            Dict with:
                - stop_reason: str ("end_turn", "tool_use", "max_tokens", etc.)
                - content: List of content blocks (text and/or tool_use)
                - model: str (the model used)
                - usage: dict (token usage info)

        Raises:
            Exception: If the API call fails
        """

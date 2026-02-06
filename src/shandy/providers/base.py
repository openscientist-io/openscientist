"""
Base provider interface and shared types.
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CostInfo:
    """Provider-agnostic cost information."""

    provider_name: str

    # Total project spending (all time)
    # None = unknown/unavailable (e.g., permissions error)
    total_spend_usd: Optional[float]

    # Recent spending (configurable time window)
    # None = unknown/unavailable (e.g., permissions error)
    recent_spend_usd: Optional[float]
    recent_period_hours: int  # e.g., 24 for "last 24h"

    # Budget tracking (optional - provider-specific)
    budget_limit_usd: Optional[float] = None
    budget_remaining_usd: Optional[float] = None

    # Data freshness
    last_updated: datetime = field(default_factory=datetime.now)
    data_lag_note: Optional[str] = None  # e.g., "Data current as of 6:35 AM ET"

    # Provider-specific extras
    key_expires: Optional[str] = None  # CBORG only
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Abstract base class for model providers."""

    def __init__(self):
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
                f"{self.name} provider configuration warnings:\n"
                + "\n".join(f"  - {warn}" for warn in warnings)
            )

    @abstractmethod
    def _validate_required_config(self) -> List[str]:
        """
        Validate required configuration.

        Returns:
            List of error messages (empty if valid)
        """
        pass

    def _validate_optional_config(self) -> List[str]:
        """
        Validate optional configuration.

        Returns:
            List of warning messages (empty if valid)
        """
        return []  # Default: no optional config

    @abstractmethod
    def setup_environment(self) -> None:
        """Configure environment variables for Claude CLI."""
        pass

    @abstractmethod
    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get project spending information.

        Args:
            lookback_hours: Time window for recent_spend_usd

        Returns:
            CostInfo with total and recent spend
        """
        pass

    def check_budget_limits(self, lookback_hours: int = 24) -> Dict[str, Any]:
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
        except Exception as e:
            logger.error(f"Could not fetch cost info for budget check: {e}")
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
            # Check total spend limit
            max_total = float(os.getenv("MAX_PROJECT_SPEND_TOTAL_USD", "inf"))
            if cost_info.total_spend_usd >= max_total:
                errors.append(
                    f"Total spend ${cost_info.total_spend_usd:.2f} "
                    f"exceeds limit ${max_total:.2f}"
                )

            # Check 24h spend limit
            max_recent = float(
                os.getenv(
                    f"MAX_PROJECT_SPEND_{lookback_hours}H_USD",
                    os.getenv("MAX_PROJECT_SPEND_24H_USD", "inf"),
                )
            )
            if cost_info.recent_spend_usd >= max_recent:
                errors.append(
                    f"Last {lookback_hours}h spend ${cost_info.recent_spend_usd:.2f} "
                    f"exceeds limit ${max_recent:.2f}"
                )

            # Check warning threshold
            warn_recent = float(
                os.getenv(
                    f"WARN_PROJECT_SPEND_{lookback_hours}H_USD",
                    os.getenv("WARN_PROJECT_SPEND_24H_USD", "inf"),
                )
            )
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
                    f"{self.name} budget exhausted " f"(${cost_info.budget_limit_usd:.2f} limit)"
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
        pass

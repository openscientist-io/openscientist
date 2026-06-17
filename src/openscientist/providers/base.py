"""Provider base hierarchy (two-axis Provider x Agent model).

A `Provider` is a model-hosting service (Anthropic, Vertex, Bedrock,
OpenAI, ...). It owns cross-family concerns: configuration validation
and cost/budget tracking. The `ClaudeCompatible` and `CodexCompatible`
subclasses add the wire-format-specific methods that let a provider be
driven by the Claude Code agent or the Codex agent respectively.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from openscientist.exceptions import ProviderError
from openscientist.models import ModelProfile, default_model_profile
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


class Provider(abc.ABC):
    """A model-hosting service. Family-specific behavior lives on the
    marker subclasses below; configuration validation and cost/budget
    tracking are shared here."""

    def __init__(self) -> None:
        """Validate configuration on construction."""
        errors = self.validate_required_config()
        if errors:
            raise ValueError(
                f"{self.display_name} provider configuration errors:\n"
                + "\n".join(f"  - {err}" for err in errors)
            )

        warnings = self._validate_optional_config()
        if warnings:
            logger.warning(
                "%s provider configuration warnings:\n%s",
                self.display_name,
                "\n".join(f"  - {warn}" for warn in warnings),
            )

    @property
    @abc.abstractmethod
    def id(self) -> str:
        """Stable identifier used by the factory selector."""

    @property
    @abc.abstractmethod
    def display_name(self) -> str: ...

    @abc.abstractmethod
    def validate_required_config(self) -> list[str]:
        """Return a list of error strings if the provider is
        misconfigured; empty list otherwise."""

    def _validate_optional_config(self) -> list[str]:
        """Return warning messages for optional misconfiguration (empty by
        default)."""
        return []

    @abc.abstractmethod
    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Return project spending information.

        Args:
            lookback_hours: Time window for recent_spend_usd

        Returns:
            CostInfo with total and recent spend
        """

    def check_budget_limits(self, lookback_hours: int = 24) -> dict[str, Any]:
        """Check if budget limits are exceeded.

        Returns:
            {"can_proceed": bool, "warnings": List[str], "errors": List[str]}
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

        return self.evaluate_budget(cost_info)

    def evaluate_budget(self, cost_info: CostInfo) -> dict[str, Any]:
        """Evaluate budget limits against pre-fetched cost info.

        Use this instead of check_budget_limits() when you already have a
        CostInfo object to avoid duplicate API calls.

        Returns:
            {"can_proceed": bool, "warnings": List[str], "errors": List[str]}
        """
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
                    f"Last {cost_info.recent_period_hours}h spend "
                    f"${cost_info.recent_spend_usd:.2f} "
                    f"exceeds limit ${max_recent:.2f}"
                )

            # Check warning threshold
            warn_recent = settings.budget.warn_project_spend_24h_usd
            if (
                cost_info.recent_spend_usd >= warn_recent
                and cost_info.recent_spend_usd < max_recent
            ):
                warnings.append(
                    f"Last {cost_info.recent_period_hours}h spend "
                    f"${cost_info.recent_spend_usd:.2f} "
                    f"approaching limit (warning threshold: ${warn_recent:.2f})"
                )

        # Provider-specific budget (e.g., CBORG max_budget)
        if cost_info.budget_remaining_usd is not None:
            if cost_info.budget_remaining_usd <= 0:
                errors.append(
                    f"{self.display_name} budget exhausted "
                    f"(${cost_info.budget_limit_usd or 0:.2f} limit)"
                )
            elif cost_info.budget_remaining_usd < 10:
                warnings.append(
                    f"{self.display_name} budget low: "
                    f"${cost_info.budget_remaining_usd:.2f} remaining"
                )

        return {"can_proceed": len(errors) == 0, "warnings": warnings, "errors": errors}

    def effective_model_name(self) -> str | None:
        """The model id this provider will actually drive, or None when it
        defers to an account/config default.

        Used for the job's model badge. Each compatibility family overrides
        this; the base returns None for a provider that has no family.
        """
        return None

    def model_profile(self) -> ModelProfile:
        """The active model's profile, chiefly its usable context window.

        The default covers hosted models (explicit override, known-model table,
        conservative default). A provider that serves a self-hosted model
        overrides this to probe the live deployment, whose num_ctx can be below
        the model's trained maximum.
        """
        return default_model_profile(
            self.effective_model_name(), get_settings().provider.model_context_tokens
        )


class ClaudeCompatible(Provider, abc.ABC):
    """Provider that speaks the Anthropic Messages API and can be driven
    by the Claude Code agent."""

    @abc.abstractmethod
    def setup_environment(self) -> None:
        """Configure process environment variables for the Claude CLI
        (auth + routing flags), clearing any conflicting flags from a
        previously-selected provider."""

    @abc.abstractmethod
    def claude_sdk_env(self) -> dict[str, str]:
        """Environment variables the claude-agent-sdk CLI must see
        (e.g., ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, AWS_REGION)."""

    @abc.abstractmethod
    def claude_model_name(self) -> str:
        """Model name to pass to ClaudeAgentOptions.model."""

    def effective_model_name(self) -> str | None:
        return self.claude_model_name()


class CodexCompatible(Provider, abc.ABC):
    """Provider that speaks the OpenAI Responses API and can be driven by
    the Codex agent."""

    @abc.abstractmethod
    def codex_config_overrides(self) -> list[str]:
        """TOML lines written into the per-job ``$CODEX_HOME/config.toml``
        for this provider — typically a ``[model_providers.<id>]`` table
        (``base_url``, ``env_key``, ``wire_api``, ``query_params``, ...).
        The codex CLI loads them from config.toml; the SDK exposes no
        programmatic config override."""

    @abc.abstractmethod
    def codex_model_name(self) -> str | None:
        """Model name passed to the codex thread (``thread_start(model=...)``). Return None to
        let codex use its account/config default (some accounts reject an
        explicit model id, e.g. ChatGPT-auth rejects ``gpt-5-codex``)."""

    @abc.abstractmethod
    def codex_model_provider_id(self) -> str:
        """The ``model_providers.<id>`` key, written as the top-level
        ``model_provider = "<id>"`` in config.toml to select this
        provider."""

    @abc.abstractmethod
    def codex_sdk_env(self) -> dict[str, str]:
        """Auth env vars the codex child must see — at minimum the secret
        named by this provider's ``model_providers.<id>.env_key``. The
        codex analog of ``claude_sdk_env()``; merged into the codex child
        environment."""

    def effective_model_name(self) -> str | None:
        return self.codex_model_name()

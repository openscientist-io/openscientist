"""
CBORG provider implementation.

Uses CBORG API for model access and cost tracking.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

import requests

from openscientist.exceptions import ProviderError
from openscientist.providers.base import ClaudeCompatible, CostInfo, LlmUpstream
from openscientist.settings import ProviderSettings, get_settings

from ._anthropic_common import anthropic_container_env
from ._env_cleanup import clear_env_vars, clear_provider_mode_flags

logger = logging.getLogger(__name__)


class CborgProvider(ClaudeCompatible):
    """
    CBORG API provider (current implementation).

    KNOWN ISSUES:
    - Newer versions of Claude CLI may send headers that CBORG doesn't recognize,
      causing HTTP 400 errors. If you encounter authentication or request errors,
      check your Claude CLI version and consider using an older version if needed.
    """

    @property
    def id(self) -> str:
        return "cborg"

    display_name = "CBORG"

    @classmethod
    def validate_model_format(cls, model: str | None) -> str | None:
        return cls.model_format_error(
            model,
            r"^claude-",
            "an Anthropic model name on CBORG (expected to start with 'claude-')",
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        errors = []
        if not provider.anthropic_auth_token:
            errors.append("ANTHROPIC_AUTH_TOKEN not set (required for CBORG)")
        if not provider.anthropic_base_url:
            errors.append("ANTHROPIC_BASE_URL not set (should be https://api.cborg.lbl.gov)")
        return errors

    def claude_sdk_env(self) -> dict[str, str]:
        """Auth env vars the claude-agent-sdk CLI must see."""
        settings = get_settings()
        env: dict[str, str] = {}
        if settings.provider.anthropic_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = settings.provider.anthropic_auth_token
        if settings.provider.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = settings.provider.anthropic_base_url
        return env

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        return anthropic_container_env(provider)

    def llm_upstream(self) -> LlmUpstream | None:
        s = get_settings().provider
        if s.anthropic_base_url and s.anthropic_auth_token:
            base = s.anthropic_base_url.rstrip("/")
            return LlmUpstream(base, {"authorization": f"Bearer {s.anthropic_auth_token}"})
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        s = get_settings().provider
        if s.anthropic_base_url and s.anthropic_auth_token:
            return {"ANTHROPIC_BASE_URL": proxy_base_url, "ANTHROPIC_AUTH_TOKEN": placeholder}
        return {}

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Route omp at the CBORG gateway.

        omp never reads ``ANTHROPIC_AUTH_TOKEN``, the name Claude Code uses for a
        gateway bearer and the one both our container env and proxy override
        publish, so untranslated it reaches the gateway with no credential at all.

        Mapped to ``ANTHROPIC_API_KEY`` rather than ``ANTHROPIC_OAUTH_TOKEN``
        because omp's OAuth path also attaches Anthropic's OAuth beta header,
        which a gateway has no reason to accept. Proxied, the proxy takes either
        header and substitutes the real bearer upstream, so this only decides the
        unproxied header form.
        """
        s = get_settings().provider
        base = proxy or s.anthropic_base_url
        env = {}
        if base:
            env["ANTHROPIC_BASE_URL"] = base
        # Proxied, the runner has already swapped in the job placeholder, so the
        # container env holds it and settings still hold the real token.
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN") if proxy else s.anthropic_auth_token
        if token:
            env["ANTHROPIC_API_KEY"] = token
        return env

    def claude_model_name(self) -> str:
        """Model name for ClaudeAgentOptions.model."""
        return get_settings().provider.model or "claude-sonnet-4-20250514"

    def _validate_optional_config(self) -> list[str]:
        """Check optional CBORG configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.model:
            warnings.append("OPENSCIENTIST_MODEL not set (will use Claude CLI default)")

        return warnings

    def setup_environment(self) -> None:
        """CBORG environment should be configured via .env and docker-compose.yml."""
        # Unset conflicting provider routing vars
        clear_provider_mode_flags(logger)
        clear_env_vars(logger, ("ANTHROPIC_API_KEY",))
        logger.info("CBORG provider initialized (configuration from environment)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get CBORG cost information.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with CBORG spend data
        """
        settings = get_settings()
        token = settings.provider.anthropic_auth_token
        if not token:
            raise ValueError("ANTHROPIC_AUTH_TOKEN not set")

        # Get total spend and budget from /key/info
        try:
            info_response = requests.get(
                "https://api.cborg.lbl.gov/key/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            info_response.raise_for_status()
            info = info_response.json()["info"]

            total_spend = info["spend"]
            max_budget = info.get("max_budget")
            key_expires = info.get("expires")

        except (requests.RequestException, KeyError, ValueError) as e:
            logger.error("Failed to fetch CBORG /key/info: %s", e)
            raise ProviderError(f"Failed to fetch CBORG /key/info: {e}") from e

        # Get recent spend from /user/daily/activity
        data_lag_note: str | None = None
        try:
            end_time = datetime.now(UTC)
            start_time = end_time - timedelta(hours=lookback_hours)

            activity_response = requests.get(
                "https://api.cborg.lbl.gov/user/daily/activity",
                params={
                    "start_date": start_time.isoformat(),
                    "end_date": end_time.isoformat(),
                    "page": "1",
                    "page_size": "1000",
                },
                headers={"x-litellm-api-key": token},
                timeout=10,
            )
            activity_response.raise_for_status()

            # Sum up costs from activity records
            activity_data = activity_response.json().get("data", [])
            recent_spend = sum(record.get("spend", 0) for record in activity_data)

        except (requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Failed to fetch CBORG activity data: %s", e)
            # Preserve "unknown" semantics so budget checks can warn appropriately.
            recent_spend = None
            data_lag_note = "Recent activity data unavailable"

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
            last_updated=datetime.now(UTC),
            data_lag_note=data_lag_note,
            key_expires=key_expires,
        )

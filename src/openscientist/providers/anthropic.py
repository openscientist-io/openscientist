"""
Direct Anthropic API provider.

For users who want to use their own Anthropic API key.
"""

import logging
import os

from openscientist.settings import ProviderSettings, get_settings

from ._anthropic_common import anthropic_container_env
from ._env_cleanup import VERTEX_PROVIDER_ENV_VARS, clear_env_vars, clear_provider_mode_flags
from .base import ClaudeCompatible, CostInfo, LlmUpstream

logger = logging.getLogger(__name__)


class AnthropicProvider(ClaudeCompatible):
    """
    Direct Anthropic API provider.

    Uses ANTHROPIC_API_KEY for authentication directly with Anthropic's API.
    This is for users who want to bring their own personal API key.
    """

    @property
    def id(self) -> str:
        return "anthropic"

    display_name = "Anthropic"

    @classmethod
    def validate_model_format(cls, model: str | None) -> str | None:
        return cls.model_format_error(
            model, r"^claude-", "an Anthropic model name (expected to start with 'claude-')"
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        if not provider.anthropic_api_key and not provider.claude_code_oauth_token:
            return [
                "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN not set. "
                "Get your API key from https://console.anthropic.com "
                "or run 'claude login' for OAuth."
            ]
        return []

    def claude_sdk_env(self) -> dict[str, str]:
        """Auth env vars the claude-agent-sdk CLI must see."""
        settings = get_settings()
        if settings.provider.claude_code_oauth_token and not settings.provider.anthropic_api_key:
            return {"CLAUDE_CODE_OAUTH_TOKEN": settings.provider.claude_code_oauth_token}
        if settings.provider.anthropic_api_key:
            return {"ANTHROPIC_API_KEY": settings.provider.anthropic_api_key}
        return {}

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        return anthropic_container_env(provider)

    def llm_upstream(self) -> LlmUpstream | None:
        p = get_settings().provider
        if p.anthropic_api_key:
            return LlmUpstream("https://api.anthropic.com", {"x-api-key": p.anthropic_api_key})
        if p.claude_code_oauth_token:
            return LlmUpstream(
                "https://api.anthropic.com",
                {"authorization": f"Bearer {p.claude_code_oauth_token}"},
            )
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        p = get_settings().provider
        if p.anthropic_api_key:
            return {"ANTHROPIC_BASE_URL": proxy_base_url, "ANTHROPIC_API_KEY": placeholder}
        if p.claude_code_oauth_token:
            return {"ANTHROPIC_BASE_URL": proxy_base_url, "CLAUDE_CODE_OAUTH_TOKEN": placeholder}
        return {}

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Route omp at Anthropic.

        ``ANTHROPIC_BASE_URL`` is spelled the same as Claude Code uses, so the
        endpoint needs no translation, but the subscription token does: omp reads
        ``ANTHROPIC_OAUTH_TOKEN`` where Claude Code reads
        ``CLAUDE_CODE_OAUTH_TOKEN``. Both the real token and the proxy placeholder
        are published under the Claude Code name, so without this omp sees no
        credential at all on the OAuth path.
        """
        p = get_settings().provider
        env = {}
        if proxy:
            env["ANTHROPIC_BASE_URL"] = proxy
        # Under the proxy the runner has replaced the token with the job
        # placeholder in the container env, so read it from there rather than
        # from settings, which still holds the real one.
        oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") if proxy else p.claude_code_oauth_token
        if oauth and not p.anthropic_api_key:
            env["ANTHROPIC_OAUTH_TOKEN"] = oauth
        return env

    def claude_model_name(self) -> str:
        """Model name for ClaudeAgentOptions.model."""
        return get_settings().provider.model or "claude-sonnet-4-20250514"

    def _validate_optional_config(self) -> list[str]:
        """Check optional configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.model:
            warnings.append("OPENSCIENTIST_MODEL not set (will use Claude CLI default)")

        return warnings

    def setup_environment(self) -> None:
        """Set up environment for Anthropic direct API or OAuth token."""
        settings = get_settings()

        # Unset conflicting provider routing vars
        clear_provider_mode_flags(logger)
        clear_env_vars(logger, VERTEX_PROVIDER_ENV_VARS)

        # If using OAuth token (from claude login), set CLAUDE_CODE_OAUTH_TOKEN
        # which is what the Claude Code CLI expects for OAuth authentication
        if settings.provider.claude_code_oauth_token and not settings.provider.anthropic_api_key:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = (
                settings.provider.claude_code_oauth_token
            )  # env-ok
            auth_method = "OAuth token (CLAUDE_CODE_OAUTH_TOKEN)"
        else:
            auth_method = "API key (ANTHROPIC_API_KEY)"

        logger.info("Anthropic provider initialized (using %s)", auth_method)

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get cost information.

        Note: Anthropic's API doesn't provide a usage/billing endpoint,
        so we return unknown for cost tracking.
        """
        return CostInfo(
            provider_name=self.display_name,
            total_spend_usd=None,
            recent_spend_usd=None,
            recent_period_hours=lookback_hours,
            data_lag_note="Cost tracking not available for direct Anthropic API",
        )

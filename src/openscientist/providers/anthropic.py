"""Direct Anthropic API provider."""

import logging
import os
from pathlib import Path

from openscientist.settings import ProviderSettings, get_settings

from ._anthropic_common import anthropic_container_env
from ._env_cleanup import VERTEX_PROVIDER_ENV_VARS
from .base import (
    AirgapEgress,
    AirgapPosture,
    ClaudeCompatible,
    CostInfo,
    LiveCheckResult,
    LlmUpstream,
    SetupQuestion,
    StaticHeadersSigning,
    _listing_live_check,
)
from .vault import vault_llm_upstream, vault_writability_warnings

logger = logging.getLogger(__name__)


class AnthropicProvider(ClaudeCompatible):
    """Direct Anthropic API provider (API key or OAuth token from claude login)."""

    @property
    def id(self) -> str:
        return "anthropic"

    display_name = "Anthropic"

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    def _validate_optional_config(self) -> list[str]:
        warnings = super()._validate_optional_config()
        p = get_settings().provider
        # The vault signs the LLM only when it is the active auth source.
        if p.omp_auth_host_path and not p.anthropic_api_key and not p.claude_code_oauth_token:
            warnings.extend(vault_writability_warnings(Path(p.omp_auth_host_path).expanduser()))
        return warnings

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        if (
            not provider.anthropic_api_key
            and not provider.claude_code_oauth_token
            and not provider.omp_auth_host_path
        ):
            return [
                "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN not set. "
                "Get your API key from https://console.anthropic.com "
                "or run 'claude login' for OAuth. "
                "Or set OMP_AUTH_HOST_PATH to the omp vault of an `omp login`."
            ]
        return []

    @classmethod
    def setup_questions(cls) -> tuple[SetupQuestion, ...]:
        return (
            SetupQuestion(
                "ANTHROPIC_API_KEY",
                kind="secret",
                help="API key from https://console.anthropic.com",
            ),
            SetupQuestion(
                "CLAUDE_CODE_OAUTH_TOKEN",
                kind="secret",
                help="or the OAuth token from `claude setup-token`",
            ),
            *super().setup_questions(),
        )

    @classmethod
    def _live_check(cls, p: ProviderSettings) -> LiveCheckResult:
        if not p.anthropic_api_key and not p.claude_code_oauth_token:
            return LiveCheckResult(
                ok=True, detail="No Anthropic credential to check with; skipped."
            )
        base = (p.anthropic_base_url or "https://api.anthropic.com").rstrip("/")
        if p.anthropic_api_key:
            headers = {"x-api-key": p.anthropic_api_key}
        else:
            headers = {"authorization": f"Bearer {p.claude_code_oauth_token}"}
        return _listing_live_check(f"{base}/v1/models", headers, cls.display_name)

    def claude_sdk_env(self) -> dict[str, str]:
        """Auth env vars the claude-agent-sdk CLI must see."""
        settings = get_settings()
        if settings.provider.claude_code_oauth_token and not settings.provider.anthropic_api_key:
            return {"CLAUDE_CODE_OAUTH_TOKEN": settings.provider.claude_code_oauth_token}
        if settings.provider.anthropic_api_key:
            return {"ANTHROPIC_API_KEY": settings.provider.anthropic_api_key}
        return {}

    @classmethod
    def container_env(cls, provider: ProviderSettings) -> dict[str, str]:
        return anthropic_container_env(provider)

    def llm_upstream(self) -> LlmUpstream | None:
        p = get_settings().provider
        if p.anthropic_api_key:
            return LlmUpstream(
                "https://api.anthropic.com",
                StaticHeadersSigning({"x-api-key": p.anthropic_api_key}),
            )
        if p.claude_code_oauth_token:
            return LlmUpstream(
                "https://api.anthropic.com",
                StaticHeadersSigning({"authorization": f"Bearer {p.claude_code_oauth_token}"}),
            )
        if p.omp_auth_host_path:
            return vault_llm_upstream(p.omp_auth_host_path, "anthropic")
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        p = get_settings().provider
        if p.anthropic_api_key:
            return {"ANTHROPIC_BASE_URL": proxy_base_url, "ANTHROPIC_API_KEY": placeholder}
        if p.claude_code_oauth_token or p.omp_auth_host_path:
            return {"ANTHROPIC_BASE_URL": proxy_base_url, "CLAUDE_CODE_OAUTH_TOKEN": placeholder}
        return {}

    @classmethod
    def airgap_posture(cls, provider: ProviderSettings) -> AirgapPosture:
        if (
            provider.anthropic_api_key
            or provider.claude_code_oauth_token
            or provider.omp_auth_host_path
        ):
            return AirgapPosture(AirgapEgress.PROXY)
        return AirgapPosture(
            AirgapEgress.UNSUPPORTED,
            reason=f"{cls.display_name} cannot be air-gapped.",
        )

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Route omp at Anthropic.

        ``ANTHROPIC_BASE_URL`` is spelled as Claude Code uses it, so the endpoint
        needs no translation. The OAuth token name differs though. omp reads
        ``ANTHROPIC_OAUTH_TOKEN``, but the real token and the proxy placeholder
        are published under Claude Code's ``CLAUDE_CODE_OAUTH_TOKEN``, so omp
        sees no credential on the OAuth path without this copy.
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

    def _conflicting_env_vars(self) -> tuple[str, ...]:
        return VERTEX_PROVIDER_ENV_VARS

    def setup_environment(self) -> None:
        super().setup_environment()

        settings = get_settings()
        # The Claude Code CLI expects the OAuth token in CLAUDE_CODE_OAUTH_TOKEN
        if settings.provider.claude_code_oauth_token and not settings.provider.anthropic_api_key:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = (
                settings.provider.claude_code_oauth_token
            )  # env-ok
            auth_method = "OAuth token (CLAUDE_CODE_OAUTH_TOKEN)"
        else:
            auth_method = "API key (ANTHROPIC_API_KEY)"

        logger.info("Anthropic provider initialized (using %s)", auth_method)

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Cost information. Anthropic's API has no billing endpoint, so
        spend is unknown."""
        return CostInfo.unavailable(
            self.display_name,
            "Cost tracking not available for direct Anthropic API",
            lookback_hours,
        )

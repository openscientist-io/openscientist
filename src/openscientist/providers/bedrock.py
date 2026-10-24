"""
AWS Bedrock provider implementation.

Uses AWS Bedrock for model access. Cost tracking via AWS Cost Explorer.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

from openscientist.providers.base import (
    AirgapEgress,
    AirgapPosture,
    ClaudeCompatible,
    CostInfo,
    LlmUpstream,
    env_from_pairs,
)
from openscientist.settings import ProviderSettings, get_settings

from ._env_cleanup import (
    VERTEX_PROVIDER_ENV_VARS,
    clear_empty_env_vars,
    clear_env_vars,
    clear_provider_mode_flags,
)

logger = logging.getLogger(__name__)


class BedrockProvider(ClaudeCompatible):
    """AWS Bedrock provider."""

    @property
    def id(self) -> str:
        return "bedrock"

    display_name = "AWS Bedrock"

    @classmethod
    def validate_model_format(cls, model: str | None) -> str | None:
        return cls.model_format_error(
            model,
            r"^([a-z]+\.anthropic\.claude-.+-v\d+:\d+|arn:aws:bedrock:)",
            "a Bedrock model id ('<region>.anthropic.claude-<name>-v<n>:<n>' or an "
            "inference-profile ARN)",
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        errors = []
        if not provider.aws_region:
            errors.append("AWS_REGION not set (e.g., us-east-1)")
        has_access_key = provider.aws_access_key_id and provider.aws_secret_access_key
        if not (has_access_key or provider.aws_profile or provider.aws_bearer_token_bedrock):
            errors.append(
                "AWS credentials not configured. Set one of: "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, or AWS_BEARER_TOKEN_BEDROCK"
            )
        return errors

    @classmethod
    def container_env(
        cls, provider: ProviderSettings, *, gcp_credentials_container_path: str | None = None
    ) -> dict[str, str]:
        env = {"CLAUDE_CODE_USE_BEDROCK": "1"}
        env.update(
            env_from_pairs(
                [
                    ("AWS_REGION", provider.aws_region),
                    ("AWS_ACCESS_KEY_ID", provider.aws_access_key_id),
                    ("AWS_SECRET_ACCESS_KEY", provider.aws_secret_access_key),
                    ("AWS_PROFILE", provider.aws_profile),
                    ("AWS_BEARER_TOKEN_BEDROCK", provider.aws_bearer_token_bedrock),
                ]
            )
        )
        return env

    def claude_sdk_env(self) -> dict[str, str]:
        """Bedrock routing/auth env for the claude-agent-sdk CLI (its container env)."""
        return type(self).container_env(get_settings().provider)

    def claude_model_name(self) -> str:
        """Model name for ClaudeAgentOptions.model."""
        return get_settings().provider.model or "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    def llm_upstream(self) -> LlmUpstream | None:
        p = get_settings().provider
        if p.aws_bearer_token_bedrock and p.aws_region:
            base = f"https://bedrock-runtime.{p.aws_region}.amazonaws.com"
            return LlmUpstream(base, {"authorization": f"Bearer {p.aws_bearer_token_bedrock}"})
        return None

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        p = get_settings().provider
        if p.aws_bearer_token_bedrock and p.aws_region:
            return {
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "ANTHROPIC_BEDROCK_BASE_URL": proxy_base_url,
                "AWS_BEARER_TOKEN_BEDROCK": placeholder,
            }
        return {}

    def proxied_container_env(
        self,
        *,
        proxy_base_url: str,
        placeholder: str,
        gcp_credentials_container_path: str | None = None,
    ) -> dict[str, str]:
        # Strip any SigV4 credential: bearer mode routes through the proxy.
        env = super().proxied_container_env(
            proxy_base_url=proxy_base_url,
            placeholder=placeholder,
            gcp_credentials_container_path=gcp_credentials_container_path,
        )
        if env.get("ANTHROPIC_BEDROCK_BASE_URL") == proxy_base_url:
            for key in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_PROFILE",
                "AWS_SESSION_TOKEN",
            ):
                env.pop(key, None)
        return env

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Route omp at Bedrock.

        omp drives Bedrock through the AWS SDK using the standard ``AWS_*``
        names, which match ours, so SigV4 needs no translation and no proxy: it
        signs its own requests and ``airgap_egress`` already reports DIRECT.

        Bearer mode is the gap. Our proxy override sets
        ``ANTHROPIC_BEDROCK_BASE_URL``, a Claude Code name omp does not read, and
        omp exposes no Bedrock base-URL override, so a proxied bearer setup
        cannot be expressed. Refuse rather than let omp reach AWS directly with
        the real token.
        """
        if not proxy:
            return {}
        raise ValueError(
            "Bedrock bearer-token auth cannot be routed through the LLM proxy under "
            "the omp harness: omp has no Bedrock base-URL override, so omp would "
            "reach AWS directly with the real token. Use SigV4 credentials "
            "(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE), which omp "
            "signs itself, or run this provider under the claude_code harness."
        )

    def airgap_egress(self) -> AirgapPosture:
        p = get_settings().provider
        if p.aws_bearer_token_bedrock:
            return AirgapPosture(AirgapEgress.PROXY)
        host = f"bedrock-runtime.{p.aws_region}.amazonaws.com"
        return AirgapPosture(AirgapEgress.DIRECT, direct_endpoints=((host, 443),))

    def _validate_optional_config(self) -> list[str]:
        """Check optional Bedrock configuration."""
        warnings = []
        settings = get_settings()

        if not settings.provider.model:
            warnings.append(
                "OPENSCIENTIST_MODEL not set (will use global.anthropic.claude-sonnet-4-5-20250929-v1:0)"
            )

        if not settings.provider.anthropic_small_fast_model:
            warnings.append(
                "ANTHROPIC_SMALL_FAST_MODEL not set (will use us.anthropic.claude-haiku-4-5-20251001-v1:0)"
            )

        return warnings

    def setup_environment(self) -> None:
        """
        Set up environment for Bedrock.

        Ensures CLAUDE_CODE_USE_BEDROCK is set and unsets conflicting
        environment variables from other providers.
        """
        # Enable Bedrock mode for Claude Code
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"  # env-ok

        # Unset conflicting provider routing vars
        clear_provider_mode_flags(logger, active_flag="CLAUDE_CODE_USE_BEDROCK")
        clear_env_vars(logger, VERTEX_PROVIDER_ENV_VARS)

        # Unset direct API key to avoid conflicts
        clear_env_vars(logger, ("ANTHROPIC_API_KEY",))

        # Unset empty vars that interfere with Bedrock auth
        # This happens when docker-compose passes VAR=${VAR} and it's unset
        empty_vars_to_clear = [
            "AWS_PROFILE",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        ]
        clear_empty_env_vars(logger, empty_vars_to_clear)

        logger.info("Bedrock provider initialized (using AWS credentials)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """
        Get Bedrock cost information from AWS Cost Explorer.

        Args:
            lookback_hours: Time window for recent spend calculation

        Returns:
            CostInfo with Bedrock spend data

        Note:
            Requires AWS Cost Explorer access. Cost data typically has
            a 24-48 hour lag in AWS.
        """
        now = datetime.now(UTC)

        # AWS Cost Explorer requires ce:GetCostAndUsage permission
        # For now, return unavailable status with instructions
        # Full implementation would use boto3 cost explorer client
        try:
            import boto3  # type: ignore[import-untyped]

            settings = get_settings()
            # Initialize Cost Explorer client
            ce_client = boto3.client("ce", region_name=settings.provider.aws_region or "us-east-1")

            # Calculate time windows (Cost Explorer requires date strings)
            end_date = now.strftime("%Y-%m-%d")
            start_date_recent = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
            # For total, go back 1 year (reasonable default)
            start_date_total = (now - timedelta(days=365)).strftime("%Y-%m-%d")

            # Query for Bedrock costs
            def get_bedrock_cost(start: str, end: str) -> float:
                response = ce_client.get_cost_and_usage(
                    TimePeriod={"Start": start, "End": end},
                    Granularity="DAILY",
                    Metrics=["UnblendedCost"],
                    Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
                )
                total = 0.0
                for result in response.get("ResultsByTime", []):
                    total += float(result["Total"]["UnblendedCost"]["Amount"])
                return total

            total_spend = get_bedrock_cost(start_date_total, end_date)
            recent_spend = get_bedrock_cost(start_date_recent, end_date)
            data_lag_note = "AWS billing data has 24-48 hour lag"

        except ImportError:
            logger.warning("boto3 not installed. Cannot fetch AWS cost data.")
            total_spend = None
            recent_spend = None
            data_lag_note = "boto3 not installed (pip install boto3)"

        except Exception as e:
            logger.warning("Could not fetch AWS cost data: %s", e)
            total_spend = None
            recent_spend = None
            data_lag_note = f"Cost data unavailable: {e}"

        settings = get_settings()
        return CostInfo(
            provider_name="AWS Bedrock",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={"region": settings.provider.aws_region},
        )

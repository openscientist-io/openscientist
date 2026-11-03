"""AWS Bedrock provider (cost tracking via AWS Cost Explorer)."""

import logging
import os
import threading
from datetime import UTC, datetime, timedelta

from openscientist.exceptions import ProviderError
from openscientist.providers.base import (
    AirgapEgress,
    AirgapPosture,
    ClaudeCompatible,
    CostInfo,
    LiveCheckResult,
    LlmUpstream,
    SetupQuestion,
    StaticHeadersSigning,
    UpstreamSigning,
    env_from_pairs,
)
from openscientist.settings import ProviderSettings, get_settings

from ._env_cleanup import VERTEX_PROVIDER_ENV_VARS, clear_empty_env_vars

logger = logging.getLogger(__name__)


class SigV4Signing(UpstreamSigning):
    """Signs each forwarded request with the web-side AWS credential (SigV4).

    The credential is resolved through botocore's default chain and refreshed
    as botocore does: each request reads its frozen value, renewing a
    refreshable credential past its staleness window. Region and service come
    from the provider settings, since the Bedrock host is fixed by them, not
    named by the request path.
    """

    def __init__(
        self, *, region: str, service: str = "bedrock-runtime", session: object = None
    ) -> None:
        if session is None:
            import botocore.session  # type: ignore[import-untyped]

            session = botocore.session.Session()
        self._session = session
        self._region = region
        self._service = service
        self._host = f"{service}.{region}.amazonaws.com"
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"https://{self._host}"

    def credential_headers(
        self, *, method: str, path: str, query: str, body: bytes
    ) -> dict[str, str]:
        import botocore.auth  # type: ignore[import-untyped]
        from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]

        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = AWSRequest(method=method.upper(), url=url)
        if body:
            request.data = body
        botocore.auth.SigV4Auth(self._credentials(), self._service, self._region).add_auth(request)
        # Lowercase the keys, matching the other signers, so the proxy's header
        # filtering (which is case-insensitive) sees a uniform shape.
        return {key.lower(): value for key, value in request.headers.items()}

    def _credentials(self) -> object:
        with self._lock:
            credentials = self._session.get_credentials()  # type: ignore[attr-defined]
            if credentials is None:
                raise ProviderError(
                    "AWS credentials are not available in the web process, so the LLM proxy "
                    "cannot sign Bedrock SigV4 requests. Set AWS_ACCESS_KEY_ID and "
                    "AWS_SECRET_ACCESS_KEY (or AWS_PROFILE) in the web environment."
                )
            # get_frozen_credentials renews a refreshable credential past its
            # staleness window, the way botocore itself refreshes it.
            return credentials.get_frozen_credentials()


#: The web-side AWS SigV4 signing, cached per (service, region) for the
#: process's life, so botocore's credential chain is resolved once.
_sigv4_signings: dict[str, "SigV4Signing"] = {}
_sigv4_signings_lock = threading.Lock()


def sigv4_signing_for(region: str, *, service: str = "bedrock-runtime") -> "SigV4Signing":
    """The web-side AWS SigV4 signing for ``region``, built once per process."""
    key = f"{service}:{region}"
    with _sigv4_signings_lock:
        signing = _sigv4_signings.get(key)
        if signing is None:
            signing = SigV4Signing(region=region, service=service)
            _sigv4_signings[key] = signing
        return signing


def _bedrock_base_url(region: str) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com"


class BedrockProvider(ClaudeCompatible):
    """AWS Bedrock provider."""

    @property
    def id(self) -> str:
        return "bedrock"

    display_name = "AWS Bedrock"

    mode_flag = "CLAUDE_CODE_USE_BEDROCK"
    default_claude_model = "us.anthropic.claude-sonnet-5-5"

    # Defense in depth: both modes proxy, so no AWS credential survives into the container.
    proxied_env_condition_key = "ANTHROPIC_BEDROCK_BASE_URL"
    proxied_env_strip_keys = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_PROFILE",
        "AWS_SESSION_TOKEN",
    )

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
    def setup_questions(cls) -> tuple[SetupQuestion, ...]:
        return (
            SetupQuestion("AWS_REGION", help="AWS region, e.g. us-east-1"),
            SetupQuestion("AWS_ACCESS_KEY_ID", kind="secret"),
            SetupQuestion("AWS_SECRET_ACCESS_KEY", kind="secret"),
            SetupQuestion("AWS_PROFILE", help="or an AWS profile, instead of access keys"),
            SetupQuestion(
                "AWS_BEARER_TOKEN_BEDROCK", kind="secret", help="or a Bedrock API bearer token"
            ),
            SetupQuestion(
                "OPENSCIENTIST_MODEL",
                help="Bedrock model id, e.g. us.anthropic.claude-sonnet-5-5-v2:0",
            ),
        )

    @classmethod
    def _live_check(cls, p: ProviderSettings) -> LiveCheckResult:
        if not p.aws_region:
            return LiveCheckResult(ok=True, detail="No AWS region to check with; skipped.")
        try:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]
        except ImportError:
            return LiveCheckResult(ok=False, detail="boto3 is not installed; cannot reach Bedrock.")
        try:
            client = boto3.client(
                "bedrock-runtime",
                region_name=p.aws_region,
                config=Config(read_timeout=10, connect_timeout=10),
            )
            client.list_foundation_models()
        except Exception as exc:  # noqa: BLE001 - surface any SDK or auth error
            return LiveCheckResult(ok=False, detail=f"Bedrock ListFoundationModels failed: {exc}")
        return LiveCheckResult(ok=True, detail="Reached AWS Bedrock (ListFoundationModels).")

    @classmethod
    def container_env(cls, provider: ProviderSettings) -> dict[str, str]:
        """The container's Bedrock routing env. It carries no AWS credential, so
        the web signs every request (SigV4) or injects the bearer, and the
        container can hold none."""
        env = {"CLAUDE_CODE_USE_BEDROCK": "1"}
        env.update(env_from_pairs([("AWS_REGION", provider.aws_region)]))
        return env

    def claude_sdk_env(self) -> dict[str, str]:
        """Bedrock routing env for the claude-agent-sdk CLI (its container env)."""
        return type(self).container_env(get_settings().provider)

    def llm_upstream(self) -> LlmUpstream | None:
        """The proxy's Bedrock upstream: the web signs (SigV4) or injects the
        bearer, and the request goes to the region's bedrock-runtime host."""
        p = get_settings().provider
        if not p.aws_region:
            return None
        if p.aws_bearer_token_bedrock:
            return LlmUpstream(
                _bedrock_base_url(p.aws_region),
                StaticHeadersSigning({"authorization": f"Bearer {p.aws_bearer_token_bedrock}"}),
            )
        signing = sigv4_signing_for(p.aws_region)
        return LlmUpstream(signing.base_url, signing)

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        """Route the container's Claude Code at the proxy's Bedrock gateway path.

        Claude Code sends the placeholder as a bearer, and the proxy signs it
        with the web's AWS credential (SigV4) or injects the real bearer."""
        p = get_settings().provider
        if not p.aws_region:
            return {}
        if p.aws_bearer_token_bedrock:
            return {
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "ANTHROPIC_BEDROCK_BASE_URL": proxy_base_url,
                "AWS_BEARER_TOKEN_BEDROCK": placeholder,
            }
        return {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BEDROCK_BASE_URL": proxy_base_url,
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "ANTHROPIC_AUTH_TOKEN": placeholder,
        }

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Refuse omp at Bedrock: there is no safe route.

        omp signs Bedrock with SigV4 (the web's real credential) and exposes
        no endpoint-override env, so the proxy cannot verify its request.
        Refuse and fail fast at launch, rather than let omp reach AWS directly.
        """
        raise ValueError(
            "Bedrock cannot be routed under the omp harness: omp signs Bedrock with "
            "SigV4 and exposes no proxy endpoint override, so omp would reach AWS "
            "directly with the web's real credential. Run this provider under the "
            "claude_code harness, which the LLM proxy signs."
        )

    @classmethod
    def airgap_posture(cls, provider: ProviderSettings) -> AirgapPosture:
        # Bedrock traffic routes through the proxy, which signs with the web's
        # credential (SigV4) or injects the bearer, so an air-gapped job
        # reaches nothing but the proxy.
        return AirgapPosture(AirgapEgress.PROXY)

    def _validate_optional_config(self) -> list[str]:
        warnings = []
        settings = get_settings()

        if not settings.provider.model:
            warnings.append("OPENSCIENTIST_MODEL not set (will use us.anthropic.claude-sonnet-5-5)")

        if not settings.provider.anthropic_small_fast_model:
            warnings.append(
                "ANTHROPIC_SMALL_FAST_MODEL not set (will use us.anthropic.claude-haiku-4-5-20251001-v1:0)"
            )

        return warnings

    def _set_own_flag(self) -> None:
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"  # env-ok

    def _conflicting_env_vars(self) -> tuple[str, ...]:
        return VERTEX_PROVIDER_ENV_VARS + ("ANTHROPIC_API_KEY",)

    def setup_environment(self) -> None:
        super().setup_environment()

        # Clear empty vars that interfere with Bedrock auth. docker-compose
        # passes VAR=${VAR} through when the var is unset.
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
        """Get Bedrock cost information from AWS Cost Explorer."""
        now = datetime.now(UTC)

        # AWS Cost Explorer requires ce:GetCostAndUsage permission
        try:
            import boto3  # type: ignore[import-untyped]

            settings = get_settings()
            ce_client = boto3.client("ce", region_name=settings.provider.aws_region or "us-east-1")

            # Cost Explorer requires date strings
            end_date = now.strftime("%Y-%m-%d")
            start_date_recent = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
            start_date_total = (now - timedelta(days=365)).strftime("%Y-%m-%d")

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

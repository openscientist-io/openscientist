"""Google Cloud Vertex AI provider (cost tracking via the GCP Billing API)."""

import logging
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from openscientist.exceptions import ProviderError
from openscientist.providers.base import (
    LLM_PROXY_URL_ENV,
    AirgapEgress,
    AirgapPosture,
    ClaudeCompatible,
    CostInfo,
    LiveCheckResult,
    LlmUpstream,
    OmpModelCatalog,
    SetupQuestion,
    UpstreamSigning,
    env_from_pairs,
)
from openscientist.settings import ProviderSettings, get_settings

if TYPE_CHECKING:
    from google.auth.credentials import Credentials as GoogleCredentials

logger = logging.getLogger(__name__)

#: The credential file the web signs with, cached per path for the process's
#: life so its access token is minted once and refreshed before expiry.
_adc_signings: dict[str, "GoogleAdcSigning"] = {}
_adc_signings_lock = threading.Lock()

# The location a Vertex request names, as /projects/{p}/locations/{loc}/...
_VERTEX_LOCATION = re.compile(r"^/projects/[^/]+/locations/([^/]+)/")


class GoogleAdcSigning(UpstreamSigning):
    """Signs each forwarded request with the web-side GCP service account.

    The access token is minted from the service account and cached inside the
    credential. It is refreshed once the credential leaves the fresh window
    (google-auth considers it stale a few minutes before expiry), under a lock
    so concurrent requests share one refresh.
    """

    def __init__(self, credentials: "GoogleCredentials") -> None:
        self._credentials = credentials
        self._lock = threading.Lock()

    def credential_headers(
        self, *, method: str, path: str, query: str, body: bytes
    ) -> dict[str, str]:
        return {"authorization": f"Bearer {self._access_token()}"}

    def _access_token(self) -> str:
        from google.auth.credentials import TokenState
        from google.auth.transport.requests import Request

        credentials = self._credentials
        with self._lock:
            if credentials.token_state != TokenState.FRESH:
                credentials.refresh(Request())  # type: ignore[no-untyped-call]
            return str(credentials.token)

    def upstream_url(self, *, base_url: str, path: str, query: str) -> str:
        # Vertex hosts one endpoint per region, so a request that names another
        # location (a VERTEX_REGION_* override) must go to that region's host.
        location = _VERTEX_LOCATION.match(path)
        base = f"https://{location.group(1)}-aiplatform.googleapis.com/v1" if location else base_url
        url = f"{base}{path}"
        return f"{url}?{query}" if query else url


def adc_signing_for(credentials_path: str) -> "GoogleAdcSigning":
    """The web-side ADC signing for the GCP credential file at ``credentials_path``,
    built once per process and path."""
    from google.oauth2 import service_account

    path = os.path.abspath(os.path.expanduser(credentials_path))
    with _adc_signings_lock:
        signing = _adc_signings.get(path)
        if signing is None:
            credentials = service_account.Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )  # type: ignore[no-untyped-call]
            signing = GoogleAdcSigning(credentials)
            _adc_signings[path] = signing
        return signing


class VertexProvider(ClaudeCompatible):
    """Google Cloud Vertex AI provider."""

    @property
    def id(self) -> str:
        return "vertex"

    display_name = "Vertex AI"

    mode_flag = "CLAUDE_CODE_USE_VERTEX"

    @classmethod
    def validate_model_format(cls, model: str | None) -> str | None:
        return cls.model_format_error(
            model, r"^claude-.+@\d{8}$", "a Vertex Anthropic model id ('claude-<name>@<YYYYMMDD>')"
        )

    def validate_required_config(self) -> list[str]:
        return self.required_config_errors(get_settings().provider)

    @classmethod
    def required_config_errors(cls, provider: ProviderSettings) -> list[str]:
        errors = []
        if not provider.anthropic_vertex_project_id:
            errors.append("ANTHROPIC_VERTEX_PROJECT_ID not set (GCP project ID)")
        # The container behind the proxy holds no GCP credential of its own;
        # only the web, which signs for it, needs one.
        if not os.environ.get(LLM_PROXY_URL_ENV):
            creds_env = provider.google_application_credentials
            if not creds_env:
                errors.append(
                    "GOOGLE_APPLICATION_CREDENTIALS not set (path to service account JSON)"
                )
            elif not os.path.exists(os.path.expanduser(creds_env)):
                errors.append(
                    f"GOOGLE_APPLICATION_CREDENTIALS file not found: {os.path.expanduser(creds_env)}"
                )
        if not provider.gcp_billing_account_id:
            errors.append("GCP_BILLING_ACCOUNT_ID not set (needed for cost tracking)")
        if not provider.cloud_ml_region:
            errors.append("CLOUD_ML_REGION not set (e.g., us-east5)")
        return errors

    def llm_upstream(self) -> LlmUpstream | None:
        """The proxy's Vertex upstream: the web's service account signs, and the
        default-region host; a request naming another location goes to that
        region's host."""
        p = get_settings().provider
        if not p.cloud_ml_region or not p.google_application_credentials:
            return None
        return LlmUpstream(
            f"https://{p.cloud_ml_region}-aiplatform.googleapis.com/v1",
            adc_signing_for(p.google_application_credentials),
        )

    @classmethod
    def setup_questions(cls) -> tuple[SetupQuestion, ...]:
        return (
            SetupQuestion("ANTHROPIC_VERTEX_PROJECT_ID", help="GCP project ID"),
            SetupQuestion(
                "GOOGLE_APPLICATION_CREDENTIALS",
                kind="path",
                help="host path to the GCP service account JSON",
            ),
            SetupQuestion("GCP_BILLING_ACCOUNT_ID", help="GCP billing account for cost tracking"),
            SetupQuestion("CLOUD_ML_REGION", help="region, e.g. us-east5"),
            *super().setup_questions(),
        )

    @classmethod
    def live_check_billed(cls) -> bool:
        # Vertex has no free listing; the check is a one-token completion.
        return True

    @classmethod
    def _live_check(cls, p: ProviderSettings) -> LiveCheckResult:
        if not p.anthropic_vertex_project_id or not p.cloud_ml_region:
            return LiveCheckResult(
                ok=True, detail="No Vertex project or region to check with; skipped."
            )
        model = p.model or cls.default_claude_model
        url = (
            "https://aiplatform.googleapis.com/v1/projects/"
            f"{p.anthropic_vertex_project_id}/locations/{p.cloud_ml_region}"
            f"/publishers/anthropic/models/{model}:rawPredict"
        )
        body = {
            "anthropic_version": "vertex-2023-10-16",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        try:
            from google.auth import default
            from google.auth.transport.requests import AuthorizedSession
        except ImportError:
            return LiveCheckResult(
                ok=False, detail="google-auth is not installed; cannot check Vertex."
            )
        try:
            credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except Exception as exc:  # noqa: BLE001 - surface any credential error
            return LiveCheckResult(ok=False, detail=f"Vertex credentials failed: {exc}")
        import requests

        try:
            with AuthorizedSession(credentials) as session:
                resp = session.post(url, json=body, timeout=30)
        except requests.RequestException as exc:
            return LiveCheckResult(ok=False, detail=f"Could not reach Vertex: {exc}")
        if not 200 <= resp.status_code < 300:
            return LiveCheckResult(
                ok=False, detail=f"Vertex check returned HTTP {resp.status_code}."
            )
        return LiveCheckResult(ok=True, detail="Reached Vertex with a one-token completion.")

    @classmethod
    def container_env(cls, provider: ProviderSettings) -> dict[str, str]:
        """The container's Vertex routing env. It carries no GCP credential, so
        the web signs every request and the container can hold none."""
        env = {"CLAUDE_CODE_USE_VERTEX": "1"}
        env.update(
            env_from_pairs(
                [
                    ("ANTHROPIC_VERTEX_PROJECT_ID", provider.anthropic_vertex_project_id),
                    ("GCP_BILLING_ACCOUNT_ID", provider.gcp_billing_account_id),
                    ("CLOUD_ML_REGION", provider.cloud_ml_region),
                    ("VERTEX_REGION_CLAUDE_4_5_SONNET", provider.vertex_region_claude_4_5_sonnet),
                    ("VERTEX_REGION_CLAUDE_4_5_HAIKU", provider.vertex_region_claude_4_5_haiku),
                ]
            )
        )
        return env

    def claude_sdk_env(self) -> dict[str, str]:
        """Vertex routing env for the claude-agent-sdk CLI (its container env)."""
        return type(self).container_env(get_settings().provider)

    def harness_env(self, *, proxy: str | None) -> dict[str, str]:
        """Route omp at Vertex through the proxy.

        omp's Vertex fetcher reads ``GOOGLE_CLOUD_ACCESS_TOKEN`` before it would
        mint a token from the default credentials, so the run's placeholder there
        is exchanged by the proxy instead of reaching Vertex directly. Without
        the proxy there is no safe route, so there is no route at all.
        """
        if not proxy:
            raise ValueError(
                "Vertex cannot be routed under the omp harness without the LLM proxy, "
                "so omp would reach Vertex directly. The proxy must be running for "
                "this harness."
            )
        # The placeholder the container's env publishes; the proxy exchanges it.
        return {"GOOGLE_CLOUD_ACCESS_TOKEN": os.environ.get("ANTHROPIC_AUTH_TOKEN", "")}

    def omp_model_catalog(self, *, context_window: int) -> OmpModelCatalog | None:
        """omp's catalog routing its Vertex model through the proxy, or None when
        the run is not proxied. The base URL is the proxy's regional Vertex
        stream path, and ``apiKey`` is the run's placeholder, which the proxy
        exchanges for the signed request."""
        model = self.effective_model_name()
        proxy = os.environ.get(LLM_PROXY_URL_ENV)
        p = get_settings().provider
        if not (proxy and model and p.anthropic_vertex_project_id and p.cloud_ml_region):
            return None
        base = (
            f"{proxy}/projects/{p.anthropic_vertex_project_id}"
            f"/locations/{p.cloud_ml_region}/publishers/anthropic/models/{model}"
        )
        return {
            "providers": {
                "google-vertex": {
                    "baseUrl": f"{base}:streamRawPredict",
                    "auth": "apiKey",
                    "api": "anthropic-messages",
                    "apiKey": os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "contextWindow": context_window,
                            "maxTokens": 32768,
                            "reasoning": True,
                            "input": ["text"],
                        }
                    ],
                }
            }
        }

    def proxy_env_overrides(self, *, proxy_base_url: str, placeholder: str) -> dict[str, str]:
        """Route the container's Claude Code at the proxy's Vertex gateway path.
        Claude Code sends the placeholder as a bearer and the proxy signs it."""
        return {
            "ANTHROPIC_VERTEX_BASE_URL": proxy_base_url,
            "CLAUDE_CODE_SKIP_VERTEX_AUTH": "1",
            "ANTHROPIC_AUTH_TOKEN": placeholder,
            LLM_PROXY_URL_ENV: proxy_base_url,
        }

    @classmethod
    def airgap_posture(cls, provider: ProviderSettings) -> AirgapPosture:
        # Vertex traffic routes through the proxy, which signs with the web's
        # credential, so an air-gapped job reaches nothing but the proxy.
        return AirgapPosture(AirgapEgress.PROXY)

    def _validate_optional_config(self) -> list[str]:
        warnings = []
        settings = get_settings()

        if not settings.provider.model:
            warnings.append("OPENSCIENTIST_MODEL not set (will use claude-sonnet-5-5)")

        if not settings.provider.vertex_region_claude_4_5_sonnet:
            warnings.append("VERTEX_REGION_CLAUDE_4_5_SONNET not set (may cause region issues)")

        if not settings.provider.vertex_region_claude_4_5_haiku:
            warnings.append("VERTEX_REGION_CLAUDE_4_5_HAIKU not set (may cause region issues)")

        return warnings

    def _conflicting_env_vars(self) -> tuple[str, ...]:
        return ("ANTHROPIC_API_KEY",)

    def setup_environment(self) -> None:
        """Vertex AI environment should be configured via .env and docker-compose.yml."""
        super().setup_environment()
        logger.info("Vertex AI provider initialized (configuration from environment)")

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Get Vertex AI cost information from the Cloud Billing API.

        Requires the BigQuery billing export enabled in GCP Console. Data has a
        1-6 hour lag in the GCP billing pipeline.
        """
        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account
        except ImportError:
            logger.error(
                "google-cloud-bigquery not installed. "
                "Install with: pip install google-cloud-bigquery"
            )
            raise

        settings = get_settings()
        creds_env = settings.provider.google_application_credentials
        if not creds_env:
            # The web signs Vertex calls from its credential; a process without
            # one (the container) cannot query GCP billing.
            return CostInfo.unavailable(
                self.display_name,
                "GCP billing query is web-side only: this process holds no GCP credential",
                lookback_hours,
            )
        creds_path = os.path.expanduser(creds_env)
        credentials = service_account.Credentials.from_service_account_file(creds_path)  # type: ignore[no-untyped-call]

        project_id = settings.provider.anthropic_vertex_project_id
        billing_account = settings.provider.gcp_billing_account_id
        if not billing_account:
            raise ProviderError("GCP_BILLING_ACCOUNT_ID not set")

        bq_client = bigquery.Client(credentials=credentials, project=project_id)

        now = datetime.now(UTC)
        recent_start = now - timedelta(hours=lookback_hours)

        billing_table = (
            f"{project_id}.billing_export.gcp_billing_export_v1_{billing_account.replace('-', '_')}"
        )

        total_query = f"""
        SELECT SUM(cost) as total_cost
        FROM `{billing_table}`
        WHERE service.description = 'Vertex AI'
          AND project.id = '{project_id}'
        """

        recent_query = f"""
        SELECT SUM(cost) as recent_cost
        FROM `{billing_table}`
        WHERE service.description = 'Vertex AI'
          AND project.id = '{project_id}'
          AND usage_start_time >= TIMESTAMP('{recent_start.isoformat()}')
        """

        try:
            total_result = list(bq_client.query(total_query).result())
            total_spend = float(total_result[0].total_cost or 0)

            recent_result = list(bq_client.query(recent_query).result())
            recent_spend = float(recent_result[0].recent_cost or 0)

            # ~3 hour average of the 1-6 hour billing delay
            lag_time = now - timedelta(hours=3)
            data_lag_note = f"Data current as of ~{lag_time.strftime('%I:%M %p %Z')}"

        except Exception as e:
            logger.warning("Could not fetch Vertex AI billing data: %s", e)
            logger.warning("Ensure BigQuery billing export is enabled in GCP Console.")
            # Return None if billing data is unavailable (permissions, export missing)
            total_spend = None
            recent_spend = None
            data_lag_note = "Billing data unavailable (ensure BigQuery export is enabled)"

        return CostInfo(
            provider_name="Vertex AI",
            total_spend_usd=total_spend,
            recent_spend_usd=recent_spend,
            recent_period_hours=lookback_hours,
            last_updated=now,
            data_lag_note=data_lag_note,
            metadata={"project_id": project_id},
        )

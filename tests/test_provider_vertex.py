"""Tests for Vertex AI provider."""

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.credentials import Credentials

from openscientist.providers.base import AirgapEgress
from openscientist.providers.vertex import GoogleAdcSigning, VertexProvider, adc_signing_for
from openscientist.settings import clear_settings_cache


def _utcnow_naive() -> datetime:
    # google-auth keeps its clocks naive-UTC, so the fake must too.
    return datetime.now(UTC).replace(tzinfo=None)


class FakeVertexCredentials(Credentials):
    """Stands in for a Google service-account credential at the token-minting
    boundary: it caches an access token and mints a new one on refresh,
    recording the mints. Token freshness comes from the real base class, so a
    short initial lifetime starts it in the refresh window."""

    def __init__(
        self, token: str = "tok-1", lifetime: float = 3600.0, refresh_lifetime: float = 3600.0
    ) -> None:
        self._token = token
        self._refresh_lifetime = refresh_lifetime
        self._expiry = _utcnow_naive() + timedelta(seconds=lifetime)
        self.refreshes: list[str] = []

    @property
    def token(self) -> str:
        return self._token

    @property
    def expiry(self) -> datetime:
        return self._expiry

    def refresh(self, request: object) -> None:
        self.refreshes.append(self._token)
        self._token = f"tok-{len(self.refreshes) + 1}"
        self._expiry = _utcnow_naive() + timedelta(seconds=self._refresh_lifetime)


def write_service_account(path: Path) -> None:
    """A service-account file google-auth accepts: a fresh RSA private key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "my-project",
                "private_key_id": "key-1",
                "private_key": pem,
                "client_email": "agent@my-project.iam.gserviceaccount.com",
                "client_id": "1234",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )


class TestGoogleAdcSigning:
    """The proxy-side Google token minting: cached, refreshed, concurrency-safe."""

    def test_the_bearer_is_the_current_access_token(self) -> None:
        credentials = FakeVertexCredentials()
        signing = GoogleAdcSigning(credentials)
        headers = signing.credential_headers(method="POST", path="/x", query="", body=b"{}")
        assert headers == {"authorization": "Bearer tok-1"}
        assert credentials.refreshes == []

    def test_the_token_is_cached_between_requests(self) -> None:
        credentials = FakeVertexCredentials()
        signing = GoogleAdcSigning(credentials)
        for _ in range(3):
            signing.credential_headers(method="POST", path="/x", query="", body=b"{}")
        assert credentials.refreshes == []

    def test_the_token_refreshes_before_expiry(self) -> None:
        credentials = FakeVertexCredentials(lifetime=100)
        signing = GoogleAdcSigning(credentials)
        first = signing.credential_headers(method="POST", path="/x", query="", body=b"{}")
        second = signing.credential_headers(method="POST", path="/x", query="", body=b"{}")
        assert credentials.refreshes == ["tok-1"]
        assert first == second == {"authorization": "Bearer tok-2"}

    def test_concurrent_requests_share_one_refresh(self) -> None:
        credentials = FakeVertexCredentials(lifetime=100)
        signing = GoogleAdcSigning(credentials)
        results: list[str] = []
        barrier = threading.Barrier(8)

        def work() -> None:
            barrier.wait()
            headers = signing.credential_headers(method="POST", path="/x", query="", body=b"{}")
            results.append(headers["authorization"])

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert credentials.refreshes == ["tok-1"]
        assert all(header == "Bearer tok-2" for header in results)

    def test_the_request_is_localized_to_the_region_in_its_path(self) -> None:
        signing = GoogleAdcSigning(FakeVertexCredentials())
        path = (
            "/projects/my-project/locations/europe-west1/"
            "publishers/anthropic/models/claude-sonnet-4-5@20250929:streamRawPredict"
        )
        assert (
            signing.upstream_url(
                base_url="https://us-east5-aiplatform.googleapis.com/v1", path=path, query=""
            )
            == "https://europe-west1-aiplatform.googleapis.com/v1/projects/my-project/"
            "locations/europe-west1/publishers/anthropic/"
            "models/claude-sonnet-4-5@20250929:streamRawPredict"
        )

    def test_a_path_without_a_region_keeps_the_base_url(self) -> None:
        signing = GoogleAdcSigning(FakeVertexCredentials())
        assert (
            signing.upstream_url(
                base_url="https://us-east5-aiplatform.googleapis.com/v1",
                path="/v1/models",
                query="pageSize=5",
            )
            == "https://us-east5-aiplatform.googleapis.com/v1/v1/models?pageSize=5"
        )


class TestVertexUpstream:
    """The proxy's upstream for Vertex: the web's service account signs."""

    def test_llm_upstream_signs_with_the_web_service_account(self, tmp_path: Path) -> None:
        creds = tmp_path / "sa.json"
        write_service_account(creds)
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            upstream = VertexProvider().llm_upstream()
        assert upstream is not None
        assert upstream.base_url == "https://us-east5-aiplatform.googleapis.com/v1"
        assert isinstance(upstream.signing, GoogleAdcSigning)
        # One signing per credential file, cached for the web process's life.
        assert upstream.signing is adc_signing_for(str(creds))

    def _valid_provider(self, tmp_path: Path) -> VertexProvider:
        creds = tmp_path / "sa.json"
        write_service_account(creds)
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            return VertexProvider()

    def test_llm_upstream_is_none_without_credentials(self, tmp_path: Path) -> None:
        provider = self._valid_provider(tmp_path)
        settings = _mock_settings(str(tmp_path / "sa.json"))
        settings.provider.google_application_credentials = None
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert provider.llm_upstream() is None

    def test_llm_upstream_is_none_without_a_region(self, tmp_path: Path) -> None:
        provider = self._valid_provider(tmp_path)
        settings = _mock_settings(str(tmp_path / "sa.json"), region=None)
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert provider.llm_upstream() is None


class TestVertexProviderValidation:
    """Tests for Vertex AI provider configuration validation."""

    def test_valid_config(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            provider = VertexProvider()
            assert "vertex" in provider.display_name.lower()

    def test_airgap_egress_is_the_proxy(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            posture = VertexProvider().airgap_egress()
        # An air-gapped Vertex job reaches only the proxy, which signs.
        assert posture.mode is AirgapEgress.PROXY
        assert posture.direct_endpoints == ()

    def test_missing_creds_file_raises(self):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/creds.json",
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            with pytest.raises(ValueError, match="not found"):
                VertexProvider()

    def test_missing_project_id_raises(self):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.provider.anthropic_vertex_project_id = None
        mock_settings.provider.google_application_credentials = "/some/creds.json"
        mock_settings.provider.gcp_billing_account_id = "id"
        mock_settings.provider.cloud_ml_region = "us-east5"
        mock_settings.provider.model = "model"
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("openscientist.providers.vertex.get_settings", return_value=mock_settings),
            patch("os.path.exists", return_value=True),
            pytest.raises(ValueError, match="ANTHROPIC_VERTEX_PROJECT_ID"),
        ):
            VertexProvider()

    def test_missing_credentials_raises(self):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.provider.anthropic_vertex_project_id = "proj"
        mock_settings.provider.google_application_credentials = None
        mock_settings.provider.gcp_billing_account_id = "id"
        mock_settings.provider.cloud_ml_region = "us-east5"
        mock_settings.provider.model = "model"
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("openscientist.providers.vertex.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="GOOGLE_APPLICATION_CREDENTIALS",
            ),
        ):
            VertexProvider()

    def test_credentials_file_not_found_raises(self):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                "GOOGLE_APPLICATION_CREDENTIALS": "/does/not/exist/creds.json",
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            with pytest.raises(ValueError, match="not found"):
                VertexProvider()

    def test_optional_warnings_missing_model(self):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.provider.anthropic_vertex_project_id = "proj"
        mock_settings.provider.google_application_credentials = "/some/creds.json"
        mock_settings.provider.gcp_billing_account_id = "id"
        mock_settings.provider.cloud_ml_region = "us-east5"
        mock_settings.provider.model = None
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("openscientist.providers.vertex.get_settings", return_value=mock_settings),
            patch("os.path.exists", return_value=True),
        ):
            # Should not raise — optional warnings don't prevent init
            provider = VertexProvider()
            assert provider.display_name == "Vertex AI"


class TestVertexSetupEnvironment:
    """Tests for Vertex AI environment setup."""

    def test_setup_does_not_raise(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            provider = VertexProvider()
            provider.setup_environment()  # should just log, not raise

    def test_setup_clears_conflicting_vars(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "ANTHROPIC_API_KEY": "sk-test",
            },
        ):
            clear_settings_cache()
            provider = VertexProvider()
            provider.setup_environment()
            assert "CLAUDE_CODE_USE_BEDROCK" not in os.environ
            assert "ANTHROPIC_API_KEY" not in os.environ


def _mock_settings(
    creds: str,
    *,
    project: str | None = "proj-123",
    billing: str | None = "BILL-1234",
    region: str | None = "us-east5",
    sonnet: str | None = "us-east5",
    haiku: str | None = "us-east5",
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.anthropic_vertex_project_id = project
    mock_settings.provider.google_application_credentials = creds
    mock_settings.provider.gcp_billing_account_id = billing
    mock_settings.provider.cloud_ml_region = region
    mock_settings.provider.vertex_region_claude_4_5_sonnet = sonnet
    mock_settings.provider.vertex_region_claude_4_5_haiku = haiku
    mock_settings.provider.model = model
    return mock_settings


class TestVertexClaudeCompatible:
    """Tests for the ClaudeCompatible family methods."""

    def _creds_file(self, tmp_path: Path) -> str:
        creds = tmp_path / "sa.json"
        creds.write_text("{}")
        return str(creds)

    def test_id_is_vertex(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path))
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert VertexProvider().id == "vertex"

    def test_display_name_is_vertex_ai(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path))
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert VertexProvider().display_name == "Vertex AI"

    def test_is_claude_compatible_and_provider(self, tmp_path: Path) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        settings = _mock_settings(self._creds_file(tmp_path))
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            provider = VertexProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path))
        assert VertexProvider.required_config_errors(settings.provider) == []

    def test_validate_required_config_errors_when_unset(self, tmp_path: Path) -> None:
        unset = _mock_settings("", project=None, billing=None, region=None)
        unset.provider.google_application_credentials = None
        errors = VertexProvider.required_config_errors(unset.provider)
        assert any("ANTHROPIC_VERTEX_PROJECT_ID" in e for e in errors)
        assert any("GOOGLE_APPLICATION_CREDENTIALS" in e for e in errors)
        assert any("GCP_BILLING_ACCOUNT_ID" in e for e in errors)
        assert any("CLOUD_ML_REGION" in e for e in errors)

    def test_claude_sdk_env_full_config(self, tmp_path: Path) -> None:
        creds = self._creds_file(tmp_path)
        settings = _mock_settings(creds)
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            env = VertexProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
            "GCP_BILLING_ACCOUNT_ID": "BILL-1234",
            "CLOUD_ML_REGION": "us-east5",
            "VERTEX_REGION_CLAUDE_4_5_SONNET": "us-east5",
            "VERTEX_REGION_CLAUDE_4_5_HAIKU": "us-east5",
        }
        # The container holds no GCP credential; the web signs for it.
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
        assert creds not in env.values()

    def test_claude_sdk_env_omits_unset_optional_regions(self, tmp_path: Path) -> None:
        creds = self._creds_file(tmp_path)
        settings = _mock_settings(creds, sonnet=None, haiku=None)
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            env = VertexProvider().claude_sdk_env()
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert "VERTEX_REGION_CLAUDE_4_5_SONNET" not in env
        assert "VERTEX_REGION_CLAUDE_4_5_HAIKU" not in env
        assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-123"

    def test_claude_model_name_uses_configured_model(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path), model="claude-custom")
        with (
            patch("openscientist.providers.vertex.get_settings", return_value=settings),
            patch("openscientist.providers.base.get_settings", return_value=settings),
        ):
            assert VertexProvider().claude_model_name() == "claude-custom"


class TestVertexContainerContext:
    """The container holds no GCP credential, so only the web validates one."""

    def test_container_context_does_not_require_the_credentials_file(self, tmp_path: Path) -> None:
        """Behind the proxy the run needs no GCP credential file at all."""
        settings = _mock_settings(str(tmp_path / "absent.json"))
        settings.provider.google_application_credentials = None
        with patch.dict(os.environ, {"OPENSCIENTIST_LLM_PROXY_URL": "http://openscientist:8081"}):
            assert VertexProvider.required_config_errors(settings.provider) == []

    def test_the_web_context_still_requires_the_credentials_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("OPENSCIENTIST_LLM_PROXY_URL", raising=False)
        settings = _mock_settings(str(tmp_path / "absent.json"))
        errors = VertexProvider.required_config_errors(settings.provider)
        assert any("GOOGLE_APPLICATION_CREDENTIALS" in e for e in errors)


class TestVertexHarness:
    """omp at Vertex is routed through the proxy, never direct."""

    def _provider(self, tmp_path: Path) -> VertexProvider:
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            return VertexProvider()

    def test_the_harness_carries_the_run_placeholder(self, tmp_path: Path, monkeypatch) -> None:
        provider = self._provider(tmp_path)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "job-1.tok")
        with patch.dict(os.environ, {"OPENSCIENTIST_LLM_PROXY_URL": "http://openscientist:8081"}):
            env = provider.harness_env(proxy="http://openscientist:8081")
        assert env == {"GOOGLE_CLOUD_ACCESS_TOKEN": "job-1.tok"}

    def test_the_harness_without_a_proxy_refuses_to_launch(self, tmp_path: Path) -> None:
        provider = self._provider(tmp_path)
        with pytest.raises(ValueError, match="proxy"):
            provider.harness_env(proxy=None)


class TestVertexOmpCatalog:
    """omp's model catalog routes Vertex through the proxy."""

    def test_the_catalog_points_the_proxy_at_the_vertex_path(self, tmp_path: Path) -> None:
        creds = tmp_path / "sa.json"
        write_service_account(creds)
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
                "OPENSCIENTIST_MODEL": "claude-sonnet-4-5@20250929",
                "OPENSCIENTIST_LLM_PROXY_URL": "http://openscientist:8081",
                "ANTHROPIC_AUTH_TOKEN": "job-1.tok",
            },
        ):
            clear_settings_cache()
            catalog = VertexProvider().omp_model_catalog(context_window=128000)
        assert catalog is not None
        entry = catalog["providers"]["google-vertex"]
        assert entry["baseUrl"] == (
            "http://openscientist:8081/projects/my-project/locations/us-east5/"
            "publishers/anthropic/models/claude-sonnet-4-5@20250929:streamRawPredict"
        )
        assert entry["auth"] == "apiKey"
        assert entry["api"] == "anthropic-messages"
        assert entry["apiKey"] == "job-1.tok"
        assert entry["models"] == [
            {
                "id": "claude-sonnet-4-5@20250929",
                "name": "claude-sonnet-4-5@20250929",
                "contextWindow": 128000,
                "maxTokens": 32768,
                "reasoning": True,
                "input": ["text"],
            }
        ]

    def test_the_catalog_is_none_without_the_proxy(self, tmp_path: Path) -> None:
        creds = tmp_path / "sa.json"
        write_service_account(creds)
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
                "OPENSCIENTIST_MODEL": "claude-sonnet-4-5@20250929",
            },
        ):
            clear_settings_cache()
            assert VertexProvider().omp_model_catalog(context_window=128000) is None


class TestVertexCostInfo:
    """Tests for the Vertex AI billing lookup."""

    def test_failed_billing_query_reports_spend_unavailable(self, tmp_path: Path) -> None:
        creds = tmp_path / "sa.json"
        creds.write_text("{}")
        settings = _mock_settings(str(creds))
        bq_client = MagicMock()
        bq_client.query.side_effect = RuntimeError("billing export table not found")
        with (
            patch("openscientist.providers.vertex.get_settings", return_value=settings),
            patch("google.oauth2.service_account.Credentials.from_service_account_file"),
            patch("google.cloud.bigquery.Client", return_value=bq_client),
        ):
            info = VertexProvider().get_cost_info(lookback_hours=6)

        assert info.total_spend_usd is None
        assert info.recent_spend_usd is None
        assert info.recent_period_hours == 6
        assert info.data_lag_note == "Billing data unavailable (ensure BigQuery export is enabled)"
        assert info.metadata == {"project_id": "proj-123"}

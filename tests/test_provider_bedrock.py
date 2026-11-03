"""Tests for AWS Bedrock provider."""

import os
from unittest.mock import MagicMock, patch

import pytest

from openscientist.providers.bedrock import BedrockProvider, SigV4Signing, sigv4_signing_for


def verify_sigv4(
    recorded: dict, *, access_key: str, secret: str, region: str, service: str = "bedrock-runtime"
) -> None:
    """Recompute the SigV4 signature over the stub's received request and check
    the ``Authorization`` header the proxy sent. A match means the forwarded
    request verifies against the web's credential."""
    from botocore.auth import SigV4Auth  # type: ignore[import-untyped]
    from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]
    from botocore.credentials import Credentials  # type: ignore[import-untyped]

    url = f"https://{recorded['host']}{recorded['path']}"
    if recorded["query"]:
        url = f"{url}?{recorded['query']}"
    request = AWSRequest(method=recorded["method"], url=url)
    if recorded["body"]:
        request.data = recorded["body"]
    request.headers["X-Amz-Date"] = recorded["headers"]["x-amz-date"]
    SigV4Auth(Credentials(access_key, secret), service, region).add_auth(request)
    assert request.headers["Authorization"] == recorded["headers"]["authorization"]


class TestBedrockProviderValidation:
    """Tests for Bedrock provider configuration validation."""

    def test_no_region_raises(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = None
        mock_settings.provider.aws_access_key_id = "key"
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="AWS_REGION",
            ),
        ):
            BedrockProvider()

    def test_no_credentials_raises(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="AWS credentials",
            ),
        ):
            BedrockProvider()

    def test_valid_access_key_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = "AKIA..."
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert "bedrock" in provider.display_name.lower()

    def test_valid_profile_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-west-2"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = "my-profile"
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert provider.display_name == "AWS Bedrock"

    def test_valid_bearer_token_config(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "eu-west-1"
        mock_settings.provider.aws_access_key_id = None
        mock_settings.provider.aws_secret_access_key = None
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = "bearer-tok"
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"

        with patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings):
            provider = BedrockProvider()
            assert provider.display_name == "AWS Bedrock"


class TestBedrockSetupEnvironment:
    """Tests for setup_environment()."""

    def _make_provider(self):
        mock_settings = MagicMock()
        mock_settings.provider.aws_region = "us-east-1"
        mock_settings.provider.aws_access_key_id = "AKIA..."
        mock_settings.provider.aws_secret_access_key = "secret"
        mock_settings.provider.aws_profile = None
        mock_settings.provider.aws_bearer_token_bedrock = None
        mock_settings.provider.model = "model"
        mock_settings.provider.anthropic_small_fast_model = "haiku"
        return mock_settings

    def test_sets_bedrock_flag(self):
        mock_settings = self._make_provider()

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {}, clear=False),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1"

    def test_clears_vertex_vars(self):
        mock_settings = self._make_provider()

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(
                os.environ,
                {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                },
            ),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert "CLAUDE_CODE_USE_VERTEX" not in os.environ
            assert "ANTHROPIC_VERTEX_PROJECT_ID" not in os.environ

    def test_clears_empty_vars(self):
        mock_settings = self._make_provider()

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(
                os.environ,
                {
                    "AWS_PROFILE": "",
                    "AWS_SESSION_TOKEN": "",
                    "ANTHROPIC_AUTH_TOKEN": "",
                },
            ),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            # Empty string vars should be removed
            assert "AWS_PROFILE" not in os.environ
            assert "AWS_SESSION_TOKEN" not in os.environ
            assert "ANTHROPIC_AUTH_TOKEN" not in os.environ

    def test_clears_api_key(self):
        mock_settings = self._make_provider()

        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=mock_settings),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
        ):
            provider = BedrockProvider()
            provider.setup_environment()
            assert "ANTHROPIC_API_KEY" not in os.environ


def _mock_settings(
    *,
    region: str | None = "us-east-1",
    access_key: str | None = "AKIATEST",
    secret: str | None = "secret-key",
    profile: str | None = None,
    bearer: str | None = None,
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.aws_region = region
    mock_settings.provider.aws_access_key_id = access_key
    mock_settings.provider.aws_secret_access_key = secret
    mock_settings.provider.aws_profile = profile
    mock_settings.provider.aws_bearer_token_bedrock = bearer
    mock_settings.provider.model = model
    return mock_settings


class TestBedrockClaudeCompatible:
    """Tests for the ClaudeCompatible family methods."""

    def test_id_is_bedrock(self) -> None:
        with patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()):
            assert BedrockProvider().id == "bedrock"

    def test_display_name_is_aws_bedrock(self) -> None:
        with patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()):
            assert BedrockProvider().display_name == "AWS Bedrock"

    def test_is_claude_compatible_and_provider(self) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        with patch("openscientist.providers.bedrock.get_settings", return_value=_mock_settings()):
            provider = BedrockProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok(self) -> None:
        assert BedrockProvider.required_config_errors(_mock_settings().provider) == []

    def test_validate_required_config_errors_when_unset(self) -> None:
        unset = _mock_settings(region=None, access_key=None, secret=None)
        errors = BedrockProvider.required_config_errors(unset.provider)
        assert len(errors) == 2
        assert any("AWS_REGION" in e for e in errors)
        assert any("credentials" in e.lower() for e in errors)

    def test_claude_sdk_env_holds_no_aws_credential(self) -> None:
        # The web signs every request, so the container env carries only the
        # routing flag and the region, never the AWS credential.
        settings = _mock_settings(region="eu-west-1", access_key="AKIA9", secret="shh")
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            env = BedrockProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "eu-west-1",
        }

    def test_claude_sdk_env_omits_bearer_and_profile(self) -> None:
        settings = _mock_settings(
            region="us-east-1",
            access_key=None,
            secret=None,
            profile="bedrock-prof",
            bearer="bt-real",
        )
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            env = BedrockProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "us-east-1",
        }

    def test_claude_model_name_uses_configured_model(self) -> None:
        settings = _mock_settings(model="bedrock-custom")
        with (
            patch("openscientist.providers.bedrock.get_settings", return_value=settings),
            patch("openscientist.providers.base.get_settings", return_value=settings),
        ):
            assert BedrockProvider().claude_model_name() == "bedrock-custom"


class TestSigV4Signing:
    """The proxy-side AWS SigV4 signing: resolves through botocore's chain,
    refreshes as botocore does, and signs the bedrock-runtime host."""

    def test_signs_with_the_web_credentials(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        signing = SigV4Signing(region="us-east-1")
        headers = signing.credential_headers(
            method="POST", path="/model/m/invoke", query="", body=b"{}"
        )
        assert headers["authorization"].startswith("AWS4-HMAC-SHA256")
        assert "/us-east-1/bedrock-runtime/aws4_request" in headers["authorization"]
        assert "x-amz-date" in headers

    def test_signature_verifies_against_the_web_credential(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        signing = SigV4Signing(region="eu-west-2")
        path = "/model/us.anthropic.claude-sonnet-5-5/invoke-with-response-stream"
        headers = signing.credential_headers(method="POST", path=path, query="", body=b"{}")
        # The stub receives the request to the regional host; verify it.
        verify_sigv4(
            {
                "method": "POST",
                "host": "bedrock-runtime.eu-west-2.amazonaws.com",
                "path": path,
                "query": "",
                "headers": {
                    "authorization": headers["authorization"],
                    "x-amz-date": headers["x-amz-date"],
                },
                "body": b"{}",
            },
            access_key="TESTAKID",
            secret="TESTSECRET",
            region="eu-west-2",
        )

    def test_a_wrong_credential_fails_verification(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        signing = SigV4Signing(region="us-east-1")
        path = "/model/m/invoke"
        headers = signing.credential_headers(method="POST", path=path, query="", body=b"{}")
        with pytest.raises(AssertionError):
            verify_sigv4(
                {
                    "method": "POST",
                    "host": "bedrock-runtime.us-east-1.amazonaws.com",
                    "path": path,
                    "query": "",
                    "headers": {
                        "authorization": headers["authorization"],
                        "x-amz-date": headers["x-amz-date"],
                    },
                    "body": b"{}",
                },
                access_key="OTHERAKID",
                secret="OTHERSECRET",
                region="us-east-1",
            )

    def test_the_session_token_is_signed(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sess-token")
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        signing = SigV4Signing(region="us-east-1")
        headers = signing.credential_headers(
            method="POST", path="/model/m/invoke", query="", body=b"{}"
        )
        assert headers["x-amz-security-token"] == "sess-token"
        assert "x-amz-security-token" in headers["authorization"].lower().split("signedheaders=")[1]

    def test_upstream_url_is_the_bedrock_host(self) -> None:
        signing = SigV4Signing(region="us-east-1")
        assert signing.base_url == "https://bedrock-runtime.us-east-1.amazonaws.com"
        assert (
            signing.upstream_url(base_url=signing.base_url, path="/model/m/invoke", query="x=1")
            == "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke?x=1"
        )

    def test_missing_credentials_refuse_to_sign(self, monkeypatch) -> None:
        for key in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
        ):
            monkeypatch.delenv(key, raising=False)

        class _EmptySession:
            def get_credentials(self):
                return None

        from openscientist.exceptions import ProviderError

        signing = SigV4Signing(region="us-east-1", session=_EmptySession())
        with pytest.raises(ProviderError, match="AWS credentials"):
            signing.credential_headers(method="POST", path="/x", query="", body=b"{}")


class _FrozenCreds:
    def __init__(self, access: str, secret: str) -> None:
        self.access_key = access
        self.secret_key = secret
        self.token = None


class _RefreshableCreds:
    """Mimics a refreshable botocore credential: each frozen read returns the
    next key in a sequence, so a refresh yields a different signature."""

    def __init__(self) -> None:
        self._seq = 0

    def get_frozen_credentials(self) -> _FrozenCreds:
        self._seq += 1
        return _FrozenCreds(f"AK-{self._seq}", f"SK-{self._seq}")


class _FakeSession:
    def __init__(self, credentials: object) -> None:
        self._credentials = credentials

    def get_credentials(self):
        return self._credentials


class TestSigV4Refresh:
    """The signing re-reads the credential's frozen value per request, the way
    botocore refreshes a credential past its staleness window."""

    def test_a_refreshed_credential_changes_the_signature(self) -> None:
        signing = SigV4Signing(region="us-east-1", session=_FakeSession(_RefreshableCreds()))
        first = signing.credential_headers(method="POST", path="/x", query="", body=b"{}")
        second = signing.credential_headers(method="POST", path="/x", query="", body=b"{}")
        assert first["authorization"] != second["authorization"]
        # Each signature verifies against the key it was frozen with.
        verify_sigv4(
            {
                "method": "POST",
                "host": "bedrock-runtime.us-east-1.amazonaws.com",
                "path": "/x",
                "query": "",
                "headers": {
                    "authorization": first["authorization"],
                    "x-amz-date": first["x-amz-date"],
                },
                "body": b"{}",
            },
            access_key="AK-1",
            secret="SK-1",
            region="us-east-1",
        )
        verify_sigv4(
            {
                "method": "POST",
                "host": "bedrock-runtime.us-east-1.amazonaws.com",
                "path": "/x",
                "query": "",
                "headers": {
                    "authorization": second["authorization"],
                    "x-amz-date": second["x-amz-date"],
                },
                "body": b"{}",
            },
            access_key="AK-2",
            secret="SK-2",
            region="us-east-1",
        )

    def test_sigv4_signing_for_caches_per_region(self) -> None:
        assert sigv4_signing_for("us-east-1") is sigv4_signing_for("us-east-1")
        assert sigv4_signing_for("us-east-1") is not sigv4_signing_for("eu-west-1")


class TestBedrockUpstream:
    """The proxy's upstream for Bedrock: the web signs (SigV4) or injects the
    bearer, and the container holds no credential of its own."""

    def test_sigv4_upstream_signs_with_the_web_credential(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
        settings = _mock_settings(region="eu-west-1", bearer=None)
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            upstream = BedrockProvider().llm_upstream()
        assert upstream is not None
        assert upstream.base_url == "https://bedrock-runtime.eu-west-1.amazonaws.com"
        assert isinstance(upstream.signing, SigV4Signing)

    def test_bearer_upstream_injects_the_bearer(self) -> None:
        settings = _mock_settings(
            region="us-east-1", access_key=None, secret=None, bearer="bt-real"
        )
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            upstream = BedrockProvider().llm_upstream()
        assert upstream is not None
        assert upstream.base_url == "https://bedrock-runtime.us-east-1.amazonaws.com"
        assert upstream.signing is not None

    def test_upstream_is_none_without_a_region(self) -> None:
        # Constructed with a valid region, then reads a region-less config:
        # no region, no host, so the proxy has no upstream to forward to.
        valid = _mock_settings(region="us-east-1", access_key="AKIA9", secret="shh")
        with patch("openscientist.providers.bedrock.get_settings", return_value=valid):
            provider = BedrockProvider()
        no_region = _mock_settings(region=None, access_key="AKIA9", secret="shh")
        with patch("openscientist.providers.bedrock.get_settings", return_value=no_region):
            assert provider.llm_upstream() is None


class TestBedrockProxiedContainerEnv:
    """The container env for Bedrock: routed at the proxy, holding no AWS
    credential, with the gateway bearer (SigV4) or the Bedrock bearer."""

    def test_sigv4_gateway_mode_routes_the_container_through_the_proxy(self) -> None:
        settings = _mock_settings(region="us-east-1", access_key="AKIA9", secret="shh", bearer=None)
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            env = BedrockProvider().proxy_env_overrides(
                proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
            )
        assert env == {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BEDROCK_BASE_URL": "http://openscientist:8081",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "ANTHROPIC_AUTH_TOKEN": "job-1.tok",
        }

    def test_bearer_mode_sets_the_bedrock_bearer(self) -> None:
        settings = _mock_settings(
            region="us-east-1", access_key=None, secret=None, bearer="bt-real"
        )
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            env = BedrockProvider().proxy_env_overrides(
                proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
            )
        assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://openscientist:8081"
        assert env["AWS_BEARER_TOKEN_BEDROCK"] == "job-1.tok"
        assert "CLAUDE_CODE_SKIP_BEDROCK_AUTH" not in env

    def test_airgap_posture_is_the_proxy(self) -> None:
        from openscientist.providers.base import AirgapEgress

        sigv4 = _mock_settings(region="us-east-1", access_key="AKIA9", secret="shh")
        assert BedrockProvider.airgap_posture(sigv4.provider).mode == AirgapEgress.PROXY
        bearer = _mock_settings(region="us-east-1", access_key=None, secret=None, bearer="bt-real")
        assert BedrockProvider.airgap_posture(bearer.provider).mode == AirgapEgress.PROXY


class TestBedrockOmpHarness:
    """omp at Bedrock is refused: it cannot be routed through the proxy, so it
    fails fast at launch with no direct egress and no in-container credential."""

    def test_omp_sigv4_is_refused(self) -> None:
        settings = _mock_settings(region="us-east-1", access_key="AKIA9", secret="shh")
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            with pytest.raises(ValueError, match="omp harness"):
                BedrockProvider().harness_env(proxy="http://openscientist:8081")

    def test_omp_bearer_is_refused(self) -> None:
        settings = _mock_settings(
            region="us-east-1", access_key=None, secret=None, bearer="bt-real"
        )
        with patch("openscientist.providers.bedrock.get_settings", return_value=settings):
            with pytest.raises(ValueError, match="omp harness"):
                BedrockProvider().harness_env(proxy="http://openscientist:8081")

"""Tests for Azure Foundry provider."""

import time
from unittest.mock import MagicMock, patch

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from openscientist.providers import foundry
from openscientist.providers.base import StaticHeadersSigning, UpstreamCredentialError
from openscientist.providers.foundry import EntraTokenSigning, FoundryProvider


class FakeAzureCredential:
    """Stands in for ``DefaultAzureCredential`` at the token-minting boundary,
    recording the scopes of every mint."""

    def __init__(self, lifetime: float = 3600.0) -> None:
        self.lifetime = lifetime
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str, **_kwargs: object) -> AccessToken:
        self.scopes.append(scopes)
        return AccessToken(f"entra-{len(self.scopes)}", int(time.time() + self.lifetime))


def _sign(signing: object) -> dict[str, str]:
    assert isinstance(signing, EntraTokenSigning)
    return signing.credential_headers(method="POST", path="/v1/messages", query="", body=b"{}")


class TestEntraTokenSigning:
    """Foundry takes an Entra ID token only as a bearer token for the ai.azure.com audience."""

    def test_signs_with_a_bearer_token_for_the_foundry_audience(self) -> None:
        credential = FakeAzureCredential()
        assert _sign(EntraTokenSigning(credential)) == {"authorization": "Bearer entra-1"}
        assert credential.scopes == [("https://ai.azure.com/.default",)]

    def test_reuses_the_token_until_it_nears_expiry(self) -> None:
        lasting = FakeAzureCredential(lifetime=3600)
        signing = EntraTokenSigning(lasting)
        assert [_sign(signing) for _ in range(3)] == [{"authorization": "Bearer entra-1"}] * 3

        expiring = FakeAzureCredential(lifetime=60)
        signing = EntraTokenSigning(expiring)
        assert _sign(signing) == {"authorization": "Bearer entra-1"}
        assert _sign(signing) == {"authorization": "Bearer entra-2"}

    def test_a_failed_mint_refuses_the_request(self) -> None:
        class _Rejecting:
            def get_token(self, *scopes: str, **_kwargs: object) -> AccessToken:
                raise ClientAuthenticationError("no account signed in")

        with pytest.raises(UpstreamCredentialError):
            _sign(EntraTokenSigning(_Rejecting()))


def test_without_an_api_key_the_proxy_signs_with_one_cached_entra_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[FakeAzureCredential] = []

    def default_credential() -> FakeAzureCredential:
        built.append(FakeAzureCredential())
        return built[-1]

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", default_credential)
    monkeypatch.setattr(foundry, "_entra_signing", None)
    with patch(
        "openscientist.providers.foundry.get_settings",
        return_value=_mock_settings(api_key=None),
    ):
        # The proxy resolves the upstream for every forwarded request.
        first = FoundryProvider().llm_upstream()
        second = FoundryProvider().llm_upstream()
    assert first is not None and second is not None
    assert first.base_url == "https://my-foundry-res.services.ai.azure.com/anthropic"
    assert _sign(first.signing) == _sign(second.signing) == {"authorization": "Bearer entra-1"}
    assert len(built) == 1


def test_with_an_api_key_the_proxy_signs_with_the_key() -> None:
    with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
        upstream = FoundryProvider().llm_upstream()
    assert upstream is not None
    assert upstream.signing == StaticHeadersSigning({"x-api-key": "foundry-key"})


def _mock_settings(
    *,
    resource: str | None = "my-foundry-res",
    base_url: str | None = None,
    api_key: str | None = "foundry-key",
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.anthropic_foundry_resource = resource
    mock_settings.provider.anthropic_foundry_base_url = base_url
    mock_settings.provider.anthropic_foundry_api_key = api_key
    mock_settings.provider.model = model
    return mock_settings


class TestFoundryClaudeCompatible:
    """Tests for the ClaudeCompatible family methods."""

    def test_id_is_foundry(self) -> None:
        with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
            assert FoundryProvider().id == "foundry"

    def test_display_name_is_azure_ai_foundry(self) -> None:
        with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
            assert FoundryProvider().display_name == "Azure AI Foundry"

    def test_is_claude_compatible_and_provider(self) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        with patch("openscientist.providers.foundry.get_settings", return_value=_mock_settings()):
            provider = FoundryProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok(self) -> None:
        assert FoundryProvider.required_config_errors(_mock_settings().provider) == []

    def test_validate_required_config_error_when_no_endpoint(self) -> None:
        no_endpoint = _mock_settings(resource=None, base_url=None, api_key="key")
        errors = FoundryProvider.required_config_errors(no_endpoint.provider)
        assert any("ANTHROPIC_FOUNDRY_RESOURCE" in e for e in errors)

    def test_claude_sdk_env_resource_mode(self) -> None:
        settings = _mock_settings(resource="res-a", base_url=None, api_key="k1")
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            env = FoundryProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_RESOURCE": "res-a",
            "ANTHROPIC_FOUNDRY_API_KEY": "k1",
        }

    def test_claude_sdk_env_base_url_mode(self) -> None:
        settings = _mock_settings(
            resource=None, base_url="https://x.services.ai.azure.com/anthropic", api_key="k2"
        )
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            env = FoundryProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://x.services.ai.azure.com/anthropic",
            "ANTHROPIC_FOUNDRY_API_KEY": "k2",
        }

    def test_claude_sdk_env_resource_wins_over_base_url(self) -> None:
        settings = _mock_settings(
            resource="res-b", base_url="https://ignored.example.com", api_key="k3"
        )
        with patch("openscientist.providers.foundry.get_settings", return_value=settings):
            env = FoundryProvider().claude_sdk_env()
        assert env["ANTHROPIC_FOUNDRY_RESOURCE"] == "res-b"
        assert "ANTHROPIC_FOUNDRY_BASE_URL" not in env

    def test_claude_model_name_uses_configured_model(self) -> None:
        settings = _mock_settings(model="foundry-custom")
        with (
            patch("openscientist.providers.foundry.get_settings", return_value=settings),
            patch("openscientist.providers.base.get_settings", return_value=settings),
        ):
            assert FoundryProvider().claude_model_name() == "foundry-custom"


def test_airgap_posture_is_unsupported_without_credentials() -> None:
    """Without an endpoint the provider cannot be routed through the proxy."""
    from types import SimpleNamespace

    from openscientist.providers.base import AirgapEgress

    posture = FoundryProvider.airgap_posture(
        SimpleNamespace(anthropic_foundry_base_url=None, anthropic_foundry_resource=None)  # type: ignore[arg-type]
    )
    assert posture.mode is AirgapEgress.UNSUPPORTED

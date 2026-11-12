"""Tests for OAuth client configuration."""

from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs

import pytest

from openscientist.auth import oauth


class FakeOAuth:
    """Capture provider registrations without contacting remote metadata endpoints."""

    def __init__(self, config: object) -> None:
        self.config = config
        self.providers: list[dict[str, object]] = []

    def register(self, **kwargs: object) -> None:
        self.providers.append(kwargs)


def _make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        auth=SimpleNamespace(
            google_client_id=None,
            google_client_secret=None,
            github_client_id=None,
            github_client_secret=None,
            orcid_client_id="APP-TEST1234567890",
            orcid_client_secret="orcid-secret",
            is_oauth_configured=True,
        ),
        dev=SimpleNamespace(dev_mode=False),
    )


def test_get_oauth_client_requests_orcid_openid_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "_oauth", None)
    monkeypatch.setattr(oauth, "get_settings", _make_settings)
    monkeypatch.setattr(oauth, "OAuth", FakeOAuth)

    client = cast(FakeOAuth, oauth.get_oauth_client())
    orcid_provider = next(provider for provider in client.providers if provider["name"] == "orcid")

    assert orcid_provider["client_kwargs"] == {"scope": "openid"}


async def test_registered_client_exchanges_an_authorization_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx2

    settings = _make_settings()
    settings.auth.github_client_id = "github-client"
    settings.auth.github_client_secret = "github-secret"
    monkeypatch.setattr(oauth, "_oauth", None)
    monkeypatch.setattr(oauth, "get_settings", lambda: settings)
    client = oauth.get_oauth_client().create_client("github")

    def exchange(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://github.com/login/oauth/access_token"
        assert request.method == "POST"
        assert request.headers["authorization"].startswith("Basic ")
        form = parse_qs(request.content.decode())
        assert form["code"] == ["authorization-code"]
        assert form["redirect_uri"] == ["https://app.example/auth/callback/github"]
        return httpx2.Response(200, json={"access_token": "provider-token", "token_type": "Bearer"})

    client.client_kwargs["transport"] = httpx2.MockTransport(exchange)
    token = await client.fetch_access_token(
        code="authorization-code", redirect_uri="https://app.example/auth/callback/github"
    )

    assert token["access_token"] == "provider-token"

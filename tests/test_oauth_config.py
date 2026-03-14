"""Tests for OAuth client configuration."""

from types import SimpleNamespace
from typing import cast

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


def test_get_oauth_client_requests_orcid_email_scope(monkeypatch) -> None:
    monkeypatch.setattr(oauth, "_oauth", None)
    monkeypatch.setattr(oauth, "get_settings", _make_settings)
    monkeypatch.setattr(oauth, "OAuth", FakeOAuth)

    client = cast(FakeOAuth, oauth.get_oauth_client())
    orcid_provider = next(provider for provider in client.providers if provider["name"] == "orcid")

    assert orcid_provider["client_kwargs"] == {"scope": "openid email"}

"""Behavioural tests for the auth HTTP routes against a real database.

The FastAPI router from ``openscientist.auth.fastapi_routes`` is exercised
through an ASGI client. The admin session opener is pointed at the
testcontainers database, while the OAuth client and the provider user-info
fetch are stubbed at their network edge.
"""

from __future__ import annotations

import re
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.fastapi_routes import router
from openscientist.auth.providers import GoogleProvider
from openscientist.database.models import Administrator, OAuthAccount, Session, User
from openscientist.settings import Settings, get_settings

MOCK_LOGIN_EMAIL = re.compile(r"^dev-[0-9a-f]{8}@example\.com$")
ADMIN_EMAIL = "admin@mock.local"
GOOGLE_EMAIL = "route-google@example.com"


def _make_app() -> FastAPI:
    """Minimal FastAPI app carrying the auth router."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


def _settings(dev_mode: bool) -> Settings:
    """Real settings with only the dev flag flipped, so the cookie and the
    redirect URIs stay identical to the production wiring."""
    real = get_settings()
    return real.model_copy(update={"dev": real.dev.model_copy(update={"dev_mode": dev_mode})})


def _admin_session_opener(db_session: AsyncSession):
    """A get_admin_session stand-in that yields the test session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    return lambda: _ctx()


def _broken_session_opener():
    """A get_admin_session stand-in that fails, standing in for an outage."""

    @asynccontextmanager
    async def _ctx():
        raise RuntimeError("database unreachable")
        yield  # unreachable, keeps this an async generator

    return lambda: _ctx()


class _StubOAuthClient:
    """Stands in for the authlib OAuth client at its network edge."""

    def __init__(
        self,
        *,
        token: dict[str, str] | None = None,
        exchange_error: Exception | None = None,
        redirect_target: str = "https://provider.example/oauth/authorize",
    ) -> None:
        self.token = token
        self.exchange_error = exchange_error
        self.redirect_target = redirect_target
        self.redirect_uri: str | None = None

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse:
        self.redirect_uri = redirect_uri
        return RedirectResponse(url=self.redirect_target, status_code=307)

    async def authorize_access_token(self, request: Request) -> dict[str, str]:
        if self.exchange_error is not None:
            raise self.exchange_error
        if self.token is None:
            raise RuntimeError("no token configured on the stub")
        return self.token


def _google_info(provider_user_id: str, email: str):
    """A provider user-info response at the OAuth network edge."""

    async def _stub(token: dict[str, str]) -> dict[str, object]:
        return {
            "provider_user_id": provider_user_id,
            "email": email,
            "name": "Route Google",
            "email_verified": True,
        }

    return _stub


async def _request(
    app: FastAPI,
    db_session: AsyncSession,
    method: str,
    path: str,
    *,
    dev_mode: bool = True,
    oauth: object | None = None,
    admin_session: object | None = None,
    data: dict[str, str] | None = None,
) -> httpx.Response:
    """Run one request with the route's settings, admin session and OAuth
    client replaced by the test stand-ins."""
    settings = _settings(dev_mode)
    opener = admin_session if admin_session is not None else _admin_session_opener(db_session)
    with ExitStack() as stack:
        stack.enter_context(patch("openscientist.auth.fastapi_routes.get_admin_session", opener))
        stack.enter_context(
            patch("openscientist.auth.fastapi_routes.get_settings", lambda: settings)
        )
        if oauth is not None:
            stack.enter_context(
                patch("openscientist.auth.fastapi_routes.get_oauth_client", lambda: oauth)
            )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, data=data)


async def _user_by_email(db_session: AsyncSession, email: str) -> User | None:
    """The user row with this email, if any."""
    result = await db_session.execute(select(User).where(User.email == email))
    return result.scalars().first()


async def _sessions_for(db_session: AsyncSession, user_id: UUID) -> list[Session]:
    """All session rows of one user."""
    result = await db_session.execute(select(Session).where(Session.user_id == user_id))
    return list(result.scalars().all())


async def _session_total(db_session: AsyncSession) -> int:
    """Number of session rows in the test database."""
    result = await db_session.execute(select(Session))
    return len(result.scalars().all())


@pytest.mark.asyncio
async def test_mock_login_creates_user_session_and_cookie(db_session: AsyncSession) -> None:
    """A mock login creates a dev user, a session row and a matching cookie."""
    app = _make_app()
    response = await _request(app, db_session, "GET", "/auth/mock/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.cookies.get("session_token")
    assert cookie is not None

    session = (
        (await db_session.execute(select(Session).where(Session.id == UUID(cookie))))
        .scalars()
        .one()
    )
    user = (
        (await db_session.execute(select(User).where(User.id == session.user_id))).scalars().one()
    )
    assert MOCK_LOGIN_EMAIL.match(user.email)
    assert session.expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_mock_login_cookie_is_http_only_and_lax(db_session: AsyncSession) -> None:
    """The session cookie must be HttpOnly, SameSite=Lax and carry a max-age."""
    app = _make_app()
    response = await _request(app, db_session, "GET", "/auth/mock/login")

    headers = response.headers.get_list("set-cookie")
    cookie_header = next(header for header in headers if header.startswith("session_token="))
    lowered = cookie_header.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "max-age=" in lowered


@pytest.mark.asyncio
async def test_mock_admin_login_marks_user_as_administrator(
    db_session: AsyncSession,
) -> None:
    """A mock admin login approves the fixed admin identity and stores an
    Administrator row."""
    app = _make_app()
    response = await _request(app, db_session, "GET", "/auth/mock/admin-login")

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    user = await _user_by_email(db_session, ADMIN_EMAIL)
    assert user is not None
    assert user.is_approved

    admins = (
        (await db_session.execute(select(Administrator).where(Administrator.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(admins) == 1

    cookie = response.cookies.get("session_token")
    assert cookie is not None
    session = (
        (await db_session.execute(select(Session).where(Session.id == UUID(cookie))))
        .scalars()
        .one()
    )
    assert session.user_id == user.id


@pytest.mark.asyncio
async def test_mock_admin_login_reuses_identity_between_calls(
    db_session: AsyncSession,
) -> None:
    """A second admin login reuses the user and admin row but issues a fresh
    session."""
    app = _make_app()
    first = await _request(app, db_session, "GET", "/auth/mock/admin-login")
    user = await _user_by_email(db_session, ADMIN_EMAIL)
    assert user is not None
    sessions_before = len(await _sessions_for(db_session, user.id))

    second = await _request(app, db_session, "GET", "/auth/mock/admin-login")

    reused = await _user_by_email(db_session, ADMIN_EMAIL)
    assert reused is not None
    assert reused.id == user.id
    admins = (
        (await db_session.execute(select(Administrator).where(Administrator.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(admins) == 1
    assert len(await _sessions_for(db_session, user.id)) == sessions_before + 1

    first_cookie = first.cookies.get("session_token")
    second_cookie = second.cookies.get("session_token")
    assert first_cookie is not None and second_cookie is not None
    assert first_cookie != second_cookie
    session = (
        (await db_session.execute(select(Session).where(Session.id == UUID(second_cookie))))
        .scalars()
        .one()
    )
    assert session.user_id == user.id


@pytest.mark.asyncio
async def test_mock_callback_uses_posted_form_data(db_session: AsyncSession) -> None:
    """A mock callback creates the user from the posted form data and links
    a mock OAuth account."""
    app = _make_app()
    response = await _request(
        app,
        db_session,
        "POST",
        "/auth/mock/callback",
        data={"email": "route-mock@example.com", "name": "Route Mock", "username": "routemock"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    user = await _user_by_email(db_session, "route-mock@example.com")
    assert user is not None
    assert user.name == "Route Mock"

    oauth_account = (
        (
            await db_session.execute(
                select(OAuthAccount).where(
                    OAuthAccount.provider == "mock",
                    OAuthAccount.provider_user_id == "mock_routemock",
                )
            )
        )
        .scalars()
        .one()
    )
    assert oauth_account.user_id == user.id

    cookie = response.cookies.get("session_token")
    assert cookie is not None
    session = (
        (await db_session.execute(select(Session).where(Session.id == UUID(cookie))))
        .scalars()
        .one()
    )
    assert session.user_id == user.id


@pytest.mark.asyncio
async def test_mock_callback_defaults_for_missing_form_fields(
    db_session: AsyncSession,
) -> None:
    """An empty form still signs the developer in under the default
    identity."""
    app = _make_app()
    response = await _request(app, db_session, "POST", "/auth/mock/callback")

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    cookie = response.cookies.get("session_token")
    assert cookie is not None
    session = (
        (await db_session.execute(select(Session).where(Session.id == UUID(cookie))))
        .scalars()
        .one()
    )
    user = (
        (await db_session.execute(select(User).where(User.id == session.user_id))).scalars().one()
    )
    assert user.email == "dev@example.com"


@pytest.mark.asyncio
async def test_mock_login_is_not_available_without_dev_mode(
    db_session: AsyncSession,
) -> None:
    """The mock login route is refused when dev mode is off."""
    app = _make_app()
    sessions_before = await _session_total(db_session)
    response = await _request(app, db_session, "GET", "/auth/mock/login", dev_mode=False)

    assert response.status_code == 404
    assert await _session_total(db_session) == sessions_before


@pytest.mark.asyncio
async def test_mock_admin_login_is_not_available_without_dev_mode(
    db_session: AsyncSession,
) -> None:
    """The mock admin login route is refused when dev mode is off."""
    app = _make_app()
    sessions_before = await _session_total(db_session)
    response = await _request(app, db_session, "GET", "/auth/mock/admin-login", dev_mode=False)

    assert response.status_code == 404
    assert await _session_total(db_session) == sessions_before


@pytest.mark.asyncio
async def test_mock_callback_is_not_available_without_dev_mode(
    db_session: AsyncSession,
) -> None:
    """The mock callback route is refused when dev mode is off."""
    app = _make_app()
    sessions_before = await _session_total(db_session)
    response = await _request(
        app,
        db_session,
        "POST",
        "/auth/mock/callback",
        dev_mode=False,
        data={"email": "route-devoff@example.com"},
    )

    assert response.status_code == 404
    assert await _session_total(db_session) == sessions_before
    assert await _user_by_email(db_session, "route-devoff@example.com") is None


@pytest.mark.asyncio
async def test_mock_callback_failure_redirects_to_login_without_session(
    db_session: AsyncSession,
) -> None:
    """A failure while creating the user must redirect to login and leave no
    user or session behind."""
    app = _make_app()
    sessions_before = await _session_total(db_session)
    response = await _request(
        app,
        db_session,
        "POST",
        "/auth/mock/callback",
        admin_session=_broken_session_opener(),
        data={"email": "route-mockfail@example.com", "name": "Broken", "username": "broken"},
    )

    assert response.status_code == 303
    assert "mock_auth_failed" in response.headers["location"]
    assert await _user_by_email(db_session, "route-mockfail@example.com") is None
    assert await _session_total(db_session) == sessions_before


@pytest.mark.asyncio
async def test_oauth_login_unknown_provider_is_refused(db_session: AsyncSession) -> None:
    """An unrecognised provider is refused before any OAuth call is made."""
    app = _make_app()
    response = await _request(app, db_session, "GET", "/auth/mystery/login")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_oauth_login_redirects_with_app_callback_uri(db_session: AsyncSession) -> None:
    """The provider redirect carries the app's callback URI for the
    provider."""
    app = _make_app()
    client = _StubOAuthClient()
    oauth = SimpleNamespace(google=client)
    response = await _request(app, db_session, "GET", "/auth/google/login", oauth=oauth)

    assert response.is_redirect
    assert response.headers["location"] == client.redirect_target
    expected = f"{get_settings().auth.app_url}/auth/google/callback"
    assert client.redirect_uri == expected


@pytest.mark.asyncio
async def test_oauth_callback_creates_user_link_and_session(
    db_session: AsyncSession,
) -> None:
    """A successful callback creates the user, the OAuth link, a session row
    and a matching cookie, without granting approval."""
    app = _make_app()
    client = _StubOAuthClient(token={"access_token": "route-tok-1"})
    oauth = SimpleNamespace(google=client)
    with patch.object(
        GoogleProvider, "get_user_info", _google_info("google-route-1", GOOGLE_EMAIL)
    ):
        response = await _request(app, db_session, "GET", "/auth/google/callback", oauth=oauth)

    assert response.is_redirect
    assert response.headers["location"] == "/"

    user = await _user_by_email(db_session, GOOGLE_EMAIL)
    assert user is not None
    assert user.is_approved is False

    oauth_account = (
        (
            await db_session.execute(
                select(OAuthAccount).where(
                    OAuthAccount.provider == "google",
                    OAuthAccount.provider_user_id == "google-route-1",
                )
            )
        )
        .scalars()
        .one()
    )
    assert oauth_account.user_id == user.id
    assert oauth_account.access_token == "route-tok-1"
    assert oauth_account.refresh_token is None

    cookie = response.cookies.get("session_token")
    assert cookie is not None
    session = (
        (await db_session.execute(select(Session).where(Session.id == UUID(cookie))))
        .scalars()
        .one()
    )
    assert session.user_id == user.id


@pytest.mark.asyncio
async def test_oauth_callback_second_login_reuses_user(db_session: AsyncSession) -> None:
    """A second callback for the same provider identity reuses the user and
    OAuth link, refreshes the stored token and issues a new session."""
    app = _make_app()
    reuse_email = "route-reuse@example.com"
    client = _StubOAuthClient(token={"access_token": "route-tok-1"})
    oauth = SimpleNamespace(google=client)
    with patch.object(GoogleProvider, "get_user_info", _google_info("google-route-2", reuse_email)):
        first = await _request(app, db_session, "GET", "/auth/google/callback", oauth=oauth)

    user = await _user_by_email(db_session, reuse_email)
    assert user is not None
    sessions_before = len(await _sessions_for(db_session, user.id))

    client2 = _StubOAuthClient(token={"access_token": "route-tok-2"})
    oauth2 = SimpleNamespace(google=client2)
    with patch.object(GoogleProvider, "get_user_info", _google_info("google-route-2", reuse_email)):
        second = await _request(app, db_session, "GET", "/auth/google/callback", oauth=oauth2)

    reused = await _user_by_email(db_session, reuse_email)
    assert reused is not None
    assert reused.id == user.id
    assert len(await _sessions_for(db_session, user.id)) == sessions_before + 1

    oauth_accounts = (
        (
            await db_session.execute(
                select(OAuthAccount).where(
                    OAuthAccount.provider == "google",
                    OAuthAccount.provider_user_id == "google-route-2",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(oauth_accounts) == 1
    assert oauth_accounts[0].user_id == user.id
    assert oauth_accounts[0].access_token == "route-tok-2"

    first_cookie = first.cookies.get("session_token")
    second_cookie = second.cookies.get("session_token")
    assert first_cookie is not None and second_cookie is not None
    assert first_cookie != second_cookie
    session = (
        (await db_session.execute(select(Session).where(Session.id == UUID(second_cookie))))
        .scalars()
        .one()
    )
    assert session.user_id == user.id


@pytest.mark.asyncio
async def test_oauth_callback_links_to_user_with_same_email(db_session: AsyncSession) -> None:
    """A callback whose email matches an existing user links the OAuth
    account to that user instead of creating a duplicate."""
    app = _make_app()
    existing = User(email="route-existing@example.com", name="Existing User")
    db_session.add(existing)
    await db_session.commit()
    await db_session.refresh(existing)

    client = _StubOAuthClient(token={"access_token": "route-tok-3"})
    oauth = SimpleNamespace(google=client)
    with patch.object(
        GoogleProvider,
        "get_user_info",
        _google_info("google-route-3", "route-existing@example.com"),
    ):
        response = await _request(app, db_session, "GET", "/auth/google/callback", oauth=oauth)

    assert response.is_redirect
    assert response.headers["location"] == "/"

    users = (
        (await db_session.execute(select(User).where(User.email == "route-existing@example.com")))
        .scalars()
        .all()
    )
    assert len(users) == 1
    assert users[0].id == existing.id

    oauth_account = (
        (
            await db_session.execute(
                select(OAuthAccount).where(
                    OAuthAccount.provider == "google",
                    OAuthAccount.provider_user_id == "google-route-3",
                )
            )
        )
        .scalars()
        .one()
    )
    assert oauth_account.user_id == existing.id


@pytest.mark.asyncio
async def test_oauth_callback_token_exchange_error_refuses_login(
    db_session: AsyncSession,
) -> None:
    """A failed token exchange redirects to login and creates no session."""
    app = _make_app()
    sessions_before = await _session_total(db_session)
    client = _StubOAuthClient(exchange_error=Exception("token endpoint refused"))
    oauth = SimpleNamespace(google=client)
    response = await _request(app, db_session, "GET", "/auth/google/callback", oauth=oauth)

    assert response.is_redirect
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["error"] == ["oauth_failed"]
    assert await _session_total(db_session) == sessions_before


@pytest.mark.asyncio
async def test_oauth_callback_missing_email_refuses_login(db_session: AsyncSession) -> None:
    """A provider response without an email is refused and leaves no partial
    state behind."""
    app = _make_app()
    sessions_before = await _session_total(db_session)
    client = _StubOAuthClient(token={"access_token": "route-tok-missing"})
    oauth = SimpleNamespace(google=client)

    async def _no_email(token: dict[str, str]) -> dict[str, object]:
        return {"provider_user_id": "google-route-4"}

    with patch.object(GoogleProvider, "get_user_info", _no_email):
        response = await _request(app, db_session, "GET", "/auth/google/callback", oauth=oauth)

    assert response.is_redirect
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["error"] == ["oauth_failed"]
    assert await _session_total(db_session) == sessions_before
    oauth_accounts = (
        (
            await db_session.execute(
                select(OAuthAccount).where(
                    OAuthAccount.provider == "google",
                    OAuthAccount.provider_user_id == "google-route-4",
                )
            )
        )
        .scalars()
        .all()
    )
    assert oauth_accounts == []


@pytest.mark.asyncio
async def test_oauth_callback_unknown_provider_refuses_login(db_session: AsyncSession) -> None:
    """An unrecognised provider on the callback path is refused without
    creating a session."""
    app = _make_app()
    sessions_before = await _session_total(db_session)
    response = await _request(app, db_session, "GET", "/auth/mystery/callback")

    assert response.status_code == 400
    assert await _session_total(db_session) == sessions_before

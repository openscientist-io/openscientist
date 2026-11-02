"""The dev-mode and OAuth login routes sign the visitor in with a session
cookie whose lifetime the admin settings row decides at the moment of login."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth import fastapi_routes
from openscientist.auth.fastapi_routes import router
from openscientist.database.models import Administrator, OAuthAccount, Session, User
from openscientist.settings import clear_settings_cache
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row


def _app() -> FastAPI:
    """A minimal app with the auth routes."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


def _mock_admin_session(db_session: AsyncSession):
    """Build a mock get_admin_session that yields the test db session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    return _ctx


@pytest.fixture
def dev_mode(monkeypatch: pytest.MonkeyPatch):
    """Dev mode on, so the mock auth routes exist."""
    monkeypatch.setenv("OPENSCIENTIST_DEV_MODE", "true")
    clear_settings_cache()
    try:
        yield
    finally:
        clear_settings_cache()


async def _user_of_token(db_session: AsyncSession, token: str) -> User:
    """The user the session behind ``token`` belongs to."""
    session_row = (
        await db_session.execute(select(Session).where(Session.id == UUID(token)))
    ).scalar_one()
    return (
        await db_session.execute(select(User).where(User.id == session_row.user_id))
    ).scalar_one()


@pytest.mark.asyncio
async def test_mock_login_sets_the_session_cookie_from_the_rows_length(
    db_session: AsyncSession, test_engine: AsyncEngine, dev_mode
):
    """The cookie's max-age is the row's session length in seconds."""
    await set_admin_settings_row(test_engine, session_duration_days=7)
    try:
        with patch.object(fastapi_routes, "get_admin_session", _mock_admin_session(db_session)):
            async with AsyncClient(
                transport=ASGITransport(app=_app()), base_url="http://test"
            ) as client:
                response = await client.get("/auth/mock/login")
    finally:
        await reset_admin_settings_row(test_engine)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie_header = response.headers["set-cookie"].lower()
    assert cookie_header.startswith("session_token=")
    assert "max-age=604800" in cookie_header
    user = await _user_of_token(db_session, response.cookies["session_token"])
    assert user.email.startswith("dev-") and user.email.endswith("@example.com")


@pytest.mark.asyncio
async def test_mock_admin_login_creates_the_admin_and_sets_the_cookie(
    db_session: AsyncSession, dev_mode
):
    """The fixed mock admin signs in approved, with an Administrator record."""
    with patch.object(fastapi_routes, "get_admin_session", _mock_admin_session(db_session)):
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            response = await client.get("/auth/mock/admin-login")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    user = await _user_of_token(db_session, response.cookies["session_token"])
    assert user.email == "admin@mock.local"
    assert user.is_approved is True
    assert (
        await db_session.execute(select(Administrator).where(Administrator.user_id == user.id))
    ).scalar_one() is not None


@pytest.mark.asyncio
async def test_mock_callback_creates_the_user_and_sets_the_cookie(
    db_session: AsyncSession, dev_mode
):
    """The mock callback form signs the form's user in."""
    with patch.object(fastapi_routes, "get_admin_session", _mock_admin_session(db_session)):
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            response = await client.post(
                "/auth/mock/callback",
                data={
                    "email": "dev-form@example.com",
                    "name": "Dev Form",
                    "username": "devform",
                },
            )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    user = await _user_of_token(db_session, response.cookies["session_token"])
    assert user.email == "dev-form@example.com"
    assert user.name == "Dev Form"


@pytest.mark.asyncio
async def test_oauth_callback_creates_the_user_and_sets_the_cookie(
    db_session: AsyncSession,
):
    """A GitHub callback stores the provider tokens and signs the user in."""
    oauth = SimpleNamespace(
        github=SimpleNamespace(
            authorize_access_token=AsyncMock(
                return_value={"access_token": "tok", "refresh_token": "ref"}
            )
        ),
        google=None,
        orcid=None,
    )

    async def _user_info(_token: dict[str, object]) -> dict[str, object]:
        return {
            "provider_user_id": "gh-1",
            "email": "gh@example.com",
            "name": "GitHub User",
            "email_verified": True,
        }

    with (
        patch.object(fastapi_routes, "get_oauth_client", lambda: oauth),
        patch.object(
            fastapi_routes,
            "GitHubProvider",
            SimpleNamespace(get_user_info=staticmethod(_user_info)),
        ),
        patch.object(fastapi_routes, "get_admin_session", _mock_admin_session(db_session)),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            response = await client.get("/auth/github/callback", params={"code": "abc"})

    assert response.status_code == 307
    assert response.headers["location"] == "/"
    user = await _user_of_token(db_session, response.cookies["session_token"])
    assert user.email == "gh@example.com"
    assert user.name == "GitHub User"
    account = (
        await db_session.execute(
            select(OAuthAccount).where(
                OAuthAccount.user_id == user.id, OAuthAccount.provider == "github"
            )
        )
    ).scalar_one()
    assert account.access_token == "tok"
    assert account.refresh_token == "ref"
    assert account.provider_user_id == "gh-1"

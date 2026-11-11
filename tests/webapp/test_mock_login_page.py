"""The mock login form signs the browser in.

The page must submit the form from the browser, so the session cookie the
callback sets lands in the browser's jar instead of in a server-side client.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nicegui import ui
from nicegui.client import Client
from nicegui.page import page
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth import fastapi_routes
from openscientist.auth.fastapi_routes import router
from openscientist.database.models import Session, User
from openscientist.database.models.session import hash_session_token
from openscientist.settings import clear_settings_cache
from openscientist.webapp_components.pages.mock_login import mock_login_form


@pytest.fixture
def dev_mode(monkeypatch: pytest.MonkeyPatch):
    """Dev mode on, so the mock auth page and routes exist."""
    monkeypatch.setenv("OPENSCIENTIST_DEV_MODE", "true")
    clear_settings_cache()
    try:
        yield
    finally:
        clear_settings_cache()


def _auth_app() -> FastAPI:
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


def test_the_mock_login_form_posts_from_the_browser(dev_mode) -> None:
    """The page renders a real HTML form the browser posts to the mock
    callback, instead of a server-side POST the cookie escapes with."""
    client = Client(page("/"))
    try:
        with client:
            mock_login_form()
        form_html = next(
            e.content
            for e in client.layout.descendants()
            if isinstance(e, ui.html) and "<form" in e.content
        )
        tag = re.search(r"<form[^>]*>", form_html)
        assert tag is not None
        assert 'method="post"' in tag.group(0)
        assert 'action="/auth/mock/callback"' in tag.group(0)
        for field in ("email", "name", "username"):
            assert f'name="{field}"' in form_html
    finally:
        client.delete()


def test_the_mock_login_form_is_refused_outside_dev_mode() -> None:
    """Without dev mode the page shows the refusal, not the form."""
    clear_settings_cache()
    try:
        client = Client(page("/"))
        try:
            with client:
                mock_login_form()
            assert not [
                e
                for e in client.layout.descendants()
                if isinstance(e, ui.html) and "<form" in e.content
            ]
        finally:
            client.delete()
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_a_browser_signing_in_through_the_form_keeps_the_cookie(
    db_session: AsyncSession, dev_mode
) -> None:
    """A browser that posts the form's fields gets the session cookie into
    its jar and can prove who the cookie signs it in as."""
    with patch.object(fastapi_routes, "get_admin_session", _mock_admin_session(db_session)):
        async with AsyncClient(
            transport=ASGITransport(app=_auth_app()),
            base_url="http://test",
            follow_redirects=False,
        ) as browser:
            response = await browser.post(
                "/auth/mock/callback",
                data={
                    "email": "browser-form@example.com",
                    "name": "Browser Form",
                    "username": "browserform",
                },
            )
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs"
    token = browser.cookies.get("session_token")
    assert token is not None
    user = await db_session.scalar(
        select(User).join(Session).where(Session.token_hash == hash_session_token(token))
    )
    assert user is not None
    assert user.email == "browser-form@example.com"

"""Redirects answered by the server, so the browser never paints a page it leaves.

Each page is built in a real NiceGUI client whose request carries only the
login cookie. A page that returns a Response has it sent as is, so the
returned 303 is what the browser receives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse

from openscientist.auth.fastapi_routes import mock_admin_oauth_login
from openscientist.auth.middleware import require_auth
from openscientist.database.models import Session, User
from openscientist.settings import clear_settings_cache
from openscientist.webapp_components.pages.admin import admin_page
from openscientist.webapp_components.pages.index import index_page
from openscientist.webapp_components.pages.login import login_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenBrowser

_ASKED_FOR = "/job/01a1aaa6-74c5-7db6-9951-30b919995b65?tab=report"


@pytest_asyncio.fixture
async def logins(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A live and an expired session of an approved non-admin, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"visitor-{uuid4().hex}@redirect.test", name="Visitor", is_approved=True)
        db.add(user)
        await db.flush()
        now = datetime.now(UTC)
        live = Session(user_id=user.id, expires_at=now + timedelta(days=1))
        expired = Session(user_id=user.id, expires_at=now - timedelta(days=1))
        db.add_all([live, expired])
        await db.commit()
    yield {"live": str(live.id), "expired": str(expired.id)}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest.fixture
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Dev mode, so the mock sign-in routes answer."""
    monkeypatch.setenv("OPENSCIENTIST_DEV_MODE", "true")
    monkeypatch.setenv("APP_URL", "http://127.0.0.1:8080")
    clear_settings_cache()
    yield
    clear_settings_cache()


def _location(response: Any) -> str:
    assert isinstance(response, RedirectResponse), response
    assert response.status_code == 303
    return response.headers["location"]


async def _protected_page_result(
    browser_client: OpenBrowser, token: str | None
) -> tuple[Any, bool]:
    ran = False

    async def page() -> None:
        nonlocal ran
        ran = True

    client = await browser_client(token, _ASKED_FOR)
    with client:
        result = await require_auth(page)()
    return result, ran


@pytest.mark.asyncio
async def test_the_home_route_redirects_to_the_jobs_list(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    client = await browser_client(logins["live"], "/")
    with client:
        assert _location(await index_page()) == "/jobs"


@pytest.mark.asyncio
async def test_a_signed_out_visit_is_sent_to_login(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    result, ran = await _protected_page_result(browser_client, None)
    assert not ran
    assert _location(result) == "/login"
    assert dict(app.storage.user) == {}


@pytest.mark.asyncio
async def test_an_expired_session_is_sent_to_login(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    result, ran = await _protected_page_result(browser_client, logins["expired"])
    assert not ran
    assert _location(result) == "/login"
    assert dict(app.storage.user) == {}


@pytest.mark.asyncio
async def test_an_anonymous_visit_writes_no_user_storage(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    """A visitor who never signs in never signs out either, so an anonymous
    visit must not create the per-browser storage file, which only a
    sign-out would ever remove."""
    result, ran = await _protected_page_result(browser_client, None)
    assert not ran
    assert _location(result) == "/login"
    assert dict(app.storage.user) == {}


@pytest.mark.asyncio
async def test_login_sends_a_signed_in_visitor_to_the_page_they_asked_for(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    client = await browser_client(logins["live"], "/login")
    with client:
        app.storage.user["return_to"] = _ASKED_FOR
        assert _location(await login_page()) == _ASKED_FOR
        assert "return_to" not in app.storage.user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remembered",
    [None, "//evil.example/x", "https://evil.example/", "/\\evil.example", "javascript:alert(1)"],
)
async def test_login_lands_on_the_jobs_list_without_a_safe_remembered_page(
    database: Forwarder,
    logins: dict[str, str],
    browser_client: OpenBrowser,
    remembered: str | None,
) -> None:
    client = await browser_client(logins["live"], "/login")
    with client:
        if remembered is not None:
            app.storage.user["return_to"] = remembered
        assert _location(await login_page()) == "/jobs"


@pytest.mark.asyncio
async def test_a_non_admin_on_the_admin_page_is_sent_to_the_jobs_list(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    client = await browser_client(logins["live"], "/admin")
    with client:
        assert _location(await admin_page()) == "/jobs"


@pytest_asyncio.fixture
async def mock_admin_cleanup(test_engine: AsyncEngine) -> AsyncIterator[None]:
    """Remove the mock admin a real sign-in commits, with its admin row and sessions."""
    yield
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.email == "admin@mock.local"))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("dev_mode", "mock_admin_cleanup")
async def test_signing_in_lands_on_the_page_the_visitor_asked_for(
    database: Forwarder, browser_client: OpenBrowser
) -> None:
    client = await browser_client(None, "/auth/mock/admin-login")
    with client:
        app.storage.user["return_to"] = _ASKED_FOR
        response = await mock_admin_oauth_login()
        assert _location(response) == _ASKED_FOR
        assert "session_token=" in response.headers["set-cookie"]
        assert "return_to" not in app.storage.user


@pytest.mark.asyncio
async def test_login_shows_the_form_when_only_stale_storage_claims_a_sign_in(
    database: Forwarder, browser_client: OpenBrowser
) -> None:
    """Storage outlives an expired cookie. Trusting it would send the visitor
    back to the protected page, which sends them to /login again, forever."""
    client = await browser_client(None, "/login")
    with client:
        app.storage.user.update({"authenticated": True, "return_to": _ASKED_FOR})
        assert await login_page() is None
    assert not [m for _target, kind, m in client.outbox.messages if kind == "open"]

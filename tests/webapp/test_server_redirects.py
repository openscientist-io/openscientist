"""Redirects answered by the server, so the browser never paints a page it leaves.

Each page is built in a real NiceGUI client whose request carries only the
login cookie. A page that returns a Response has it sent as is, so the
returned 303 is what the browser receives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import RedirectResponse, Response

from openscientist.auth.fastapi_routes import mock_admin_oauth_login, mock_oauth_login
from openscientist.auth.middleware import require_auth
from openscientist.database.models import Session, User
from openscientist.database.models.session import hash_session_token
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
        live, live_token = Session.issue(user_id=user.id, expires_at=now + timedelta(days=1))
        expired, expired_token = Session.issue(user_id=user.id, expires_at=now - timedelta(days=1))
        db.add_all([live, expired])
        await db.commit()
    yield {"live": live_token, "expired": expired_token}
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


def _location(response: Response | None) -> str:
    assert isinstance(response, RedirectResponse), response
    assert response.status_code == 303
    return response.headers["location"]


def _session_token(response: Response) -> str:
    """The session token a sign-in response sets on the login cookie."""
    cookie = response.headers["set-cookie"]
    assert "session_token=" in cookie, cookie
    return cookie.split("session_token=", 1)[1].split(";", 1)[0]


async def _protected_page_result(
    browser_client: OpenBrowser, token: str | None
) -> tuple[Response | None, bool]:
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
async def test_a_signed_out_visit_is_sent_to_login_with_the_url_as_next(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    result, ran = await _protected_page_result(browser_client, None)
    assert not ran
    assert _location(result) == "/login?next=" + quote(_ASKED_FOR)
    assert dict(app.storage.user) == {}


@pytest.mark.asyncio
async def test_an_expired_session_is_sent_to_login_with_the_url_as_next(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    result, ran = await _protected_page_result(browser_client, logins["expired"])
    assert not ran
    assert _location(result) == "/login?next=" + quote(_ASKED_FOR)
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
    assert _location(result) == "/login?next=" + quote(_ASKED_FOR)
    assert dict(app.storage.user) == {}


@pytest.mark.asyncio
async def test_login_sends_a_signed_in_visitor_to_the_page_they_asked_for(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    client = await browser_client(logins["live"], "/login?next=" + quote(_ASKED_FOR))
    with client:
        assert _location(await login_page()) == _ASKED_FOR


@pytest.mark.asyncio
async def test_login_refuses_a_signed_in_visitor_next_that_names_an_auth_route(
    database: Forwarder, logins: dict[str, str], browser_client: OpenBrowser
) -> None:
    """A signed-in visitor who asks for /login carrying an auth route as next
    is sent to the home page, not the internal route that would swap their
    session."""
    client = await browser_client(logins["live"], "/login?next=" + quote("/auth/mock/admin-login"))
    with client:
        assert _location(await login_page()) == "/jobs"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_path",
    [None, "//evil.example/x", "https://evil.example/", "/\\evil.example", "javascript:alert(1)"],
)
async def test_login_lands_on_the_jobs_list_without_a_safe_next(
    database: Forwarder,
    logins: dict[str, str],
    browser_client: OpenBrowser,
    next_path: str | None,
) -> None:
    url = "/login?next=" + quote(next_path) if next_path is not None else "/login"
    client = await browser_client(logins["live"], url)
    with client:
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


@pytest_asyncio.fixture
async def mock_dev_cleanup(test_engine: AsyncEngine) -> AsyncIterator[None]:
    """Remove the mock dev users a sign-in commits, with their sessions."""
    yield
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.email.like("dev-%")))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("dev_mode", "mock_admin_cleanup")
async def test_signing_in_lands_on_the_page_the_visitor_asked_for(
    database: Forwarder, browser_client: OpenBrowser
) -> None:
    client = await browser_client(None, "/auth/mock/admin-login?next=" + quote(_ASKED_FOR))
    with client:
        response = await mock_admin_oauth_login(client.request)
        assert _location(response) == _ASKED_FOR
        assert "session_token=" in response.headers["set-cookie"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("dev_mode", "mock_admin_cleanup")
async def test_two_tabs_of_one_browser_land_on_their_own_pages(
    database: Forwarder, browser_client: OpenBrowser
) -> None:
    """Each tab of a browser carries the page it asked for in its own login
    URL, so one tab's sign-in lands on that tab's page, not the other
    tab's."""
    browser_id = str(uuid4())
    asked_a = await browser_client(None, _ASKED_FOR, browser_id)
    asked_b = await browser_client(None, "/admin", browser_id)

    async def page_a() -> None:
        return None

    async def page_b() -> None:
        return None

    with asked_a:
        redirect_a = await require_auth(page_a)()
    with asked_b:
        redirect_b = await require_auth(page_b)()

    assert _location(redirect_a) == "/login?next=" + quote(_ASKED_FOR)
    assert _location(redirect_b) == "/login?next=" + quote("/admin")
    assert dict(app.storage.user) == {}

    # Each tab's login page sends its own browser to its own sign-in URL.
    sign_in_a = await browser_client(
        None, "/auth/mock/admin-login?next=" + quote(_ASKED_FOR), browser_id
    )
    sign_in_b = await browser_client(
        None, "/auth/mock/admin-login?next=" + quote("/admin"), browser_id
    )
    response_a = await mock_admin_oauth_login(sign_in_a.request)
    response_b = await mock_admin_oauth_login(sign_in_b.request)
    assert _location(response_a) == _ASKED_FOR
    assert _location(response_b) == "/admin"
    assert "session_token=" in response_a.headers["set-cookie"]
    assert "session_token=" in response_b.headers["set-cookie"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("dev_mode", "mock_dev_cleanup")
async def test_signing_in_refuses_a_next_that_names_the_admin_login(
    database: Forwarder,
    test_engine: AsyncEngine,
    browser_client: OpenBrowser,
) -> None:
    """A next that names the admin sign-in route must not swap the visitor
    into the admin's session. The sign-in lands on the home page with the
    visitor's own, non-admin, session."""
    client = await browser_client(None, "/auth/mock/login?next=" + quote("/auth/mock/admin-login"))
    with client:
        response = await mock_oauth_login(client.request)
        assert _location(response) == "/jobs"
        token = _session_token(response)
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        email = (
            await db.execute(
                select(User.email)
                .join(Session, Session.user_id == User.id)
                .where(Session.token_hash == hash_session_token(token))
            )
        ).scalar_one()
    assert email.startswith("dev-")
    assert email != "admin@mock.local"


@pytest.mark.asyncio
@pytest.mark.usefixtures("dev_mode", "mock_dev_cleanup")
@pytest.mark.parametrize(
    "next_path",
    [
        "/auth/mock/login",
        "/auth/mock/admin-login",
        "/auth/google/login",
        "/auth/google/callback",
        "/auth/logout",
        "/logout",
        "/logout/extra",
        "/%61uth/mock/admin-login",
        "/auth%2Fmock%2Fadmin-login",
        "/auth?next=/jobs",
        "/jobs\r\nSet-Cookie:evil=1",
        "/./auth/mock/admin-login",
        "/jobs/../auth/mock/admin-login",
        "/../auth/mock/admin-login",
        "/%2e/auth/mock/admin-login",
        "/jobs/%2E%2E/auth/x",
        "/auth//..",
        "/auth/mock/admin-login/x////../../../..",
    ],
)
async def test_signing_in_refuses_an_internal_or_controlled_target(
    database: Forwarder,
    test_engine: AsyncEngine,
    browser_client: OpenBrowser,
    next_path: str,
) -> None:
    """A next that names a sign-in or sign-out route, encodes one, folds to
    one through dot segments, or carries a control character lands on the
    home page with the visitor's own session, not the internal target."""
    client = await browser_client(None, "/auth/mock/login?next=" + quote(next_path))
    with client:
        response = await mock_oauth_login(client.request)
        assert _location(response) == "/jobs"
        token = _session_token(response)
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        email = (
            await db.execute(
                select(User.email)
                .join(Session, Session.user_id == User.id)
                .where(Session.token_hash == hash_session_token(token))
            )
        ).scalar_one()
    assert email.startswith("dev-")
    assert email != "admin@mock.local"


@pytest.mark.asyncio
@pytest.mark.usefixtures("dev_mode", "mock_dev_cleanup")
@pytest.mark.parametrize(
    "asked",
    [
        "/job/01a1aaa6-74c5-7db6-9951-30b919995b65?x=1",
        "/auth/../jobs",
        "/jobs/./x",
    ],
)
async def test_signing_in_keeps_the_page_the_visitor_asked_for(
    database: Forwarder, browser_client: OpenBrowser, asked: str
) -> None:
    """A legitimate page, including one whose dot segments fold to the jobs
    list, survives the sign-in round trip with its target unchanged."""
    client = await browser_client(None, "/auth/mock/login?next=" + quote(asked))
    with client:
        response = await mock_oauth_login(client.request)
        assert _location(response) == asked


@pytest.mark.asyncio
async def test_login_shows_the_form_when_only_stale_storage_claims_a_sign_in(
    database: Forwarder, browser_client: OpenBrowser
) -> None:
    """Storage outlives an expired cookie. Trusting it would send the visitor
    back to the protected page, which sends them to /login again, forever."""
    client = await browser_client(None, "/login")
    with client:
        app.storage.user["authenticated"] = True
        assert await login_page() is None
    assert not [m for _target, kind, m in client.outbox.messages if kind == "open"]

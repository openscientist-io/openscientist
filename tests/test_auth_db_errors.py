"""Sign-in checks and logout under a real database outage
(plans/auth-db-errors.md)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth import middleware
from openscientist.auth.fastapi_routes import router
from openscientist.database.models import Session, User
from tests.db_outage import Forwarder


@pytest_asyncio.fixture
async def session_token(test_engine: AsyncEngine) -> AsyncGenerator[UUID, None]:
    """A signed-in visitor's valid session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"auth-{uuid4().hex}@outage.test", name="Visitor", is_approved=True)
        db.add(user)
        await db.flush()
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield login.id
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest.fixture
def visitor(session_token: UUID, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """The NiceGUI context of a visitor carrying ``session_token``. Returns
    their stored session, which a sign-out would clear."""
    storage: dict[str, object] = {"session_token": str(session_token), "user_id": "kept"}
    ui = MagicMock()
    ui.context.client.request.cookies = {"session_token": str(session_token)}
    ui.context.client.page.path = "/jobs"
    ui.context.client.page.response_timeout = _RESPONSE_TIMEOUT
    monkeypatch.setattr(middleware, "app", SimpleNamespace(storage=SimpleNamespace(user=storage)))
    monkeypatch.setattr(middleware, "ui", ui)
    return storage


async def test_a_page_during_an_outage_keeps_the_visitor_signed_in(
    database: Forwarder, visitor: dict[str, object]
) -> None:
    ran = False

    async def page() -> None:
        nonlocal ran
        ran = True

    stored = dict(visitor)
    database.down()
    await middleware.require_auth(page)()
    assert not ran
    assert visitor == stored
    middleware.ui.navigate.to.assert_not_called()  # type: ignore[attr-defined]


_RESPONSE_TIMEOUT = 1.5
_OUTAGE = "OpenScientist cannot reach its database right now."


async def _build_within_test_bound(page: Callable[[], Awaitable[None]]) -> float:
    """Build ``page`` through ``require_auth``, returning how long it took. The
    test fails, instead of waiting out asyncpg's 60 s connect timeout, when the
    build has no bound of its own."""
    started = time.monotonic()
    await asyncio.wait_for(middleware.require_auth(page)(), timeout=10)
    return time.monotonic() - started


async def test_a_page_while_the_database_hangs_shows_the_outage_page_in_time(
    database: Forwarder, visitor: dict[str, object]
) -> None:
    ran = False

    async def page() -> None:
        nonlocal ran
        ran = True

    stored = dict(visitor)
    database.hang()
    took = await _build_within_test_bound(page)
    assert took < _RESPONSE_TIMEOUT
    assert not ran
    middleware.ui.label.assert_any_call(_OUTAGE)  # type: ignore[attr-defined]
    assert visitor == stored


async def test_a_hang_after_the_session_check_also_shows_the_outage_page(
    database: Forwarder, visitor: dict[str, object]
) -> None:
    finished = False

    async def page() -> None:
        nonlocal finished
        database.hang()
        await middleware.validate_session(str(visitor["session_token"]))
        finished = True

    took = await _build_within_test_bound(page)
    assert took < _RESPONSE_TIMEOUT
    assert not finished
    middleware.ui.label.assert_any_call(_OUTAGE)  # type: ignore[attr-defined]


async def test_a_timeout_raised_by_the_page_itself_is_not_an_outage(
    database: Forwarder, visitor: dict[str, object]
) -> None:
    async def page() -> None:
        raise TimeoutError("the page's own deadline")

    with pytest.raises(TimeoutError, match="the page's own deadline"):
        await middleware.require_auth(page)()
    assert call(_OUTAGE) not in middleware.ui.label.call_args_list  # type: ignore[attr-defined]


async def test_a_signed_in_visitor_reaches_the_page(
    database: Forwarder, visitor: dict[str, object]
) -> None:
    """The database is up and the token is real, so the visitor gets the
    page and their user lands in the storage the page reads."""
    ran = False

    async def page() -> None:
        nonlocal ran
        ran = True

    await middleware.require_auth(page)()
    assert ran
    assert visitor["authenticated"] is True
    assert visitor["name"] == "Visitor"
    assert visitor["is_admin"] is False
    assert visitor["can_start_jobs"] is True
    middleware.ui.navigate.to.assert_not_called()  # type: ignore[attr-defined]


async def test_a_visitor_with_a_garbage_token_is_sent_to_login(
    database: Forwarder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token the database does not know is a signed-out visitor, not an
    outage, so the page is withheld and the stored session is cleared."""
    ran = False

    async def page() -> None:
        nonlocal ran
        ran = True

    token = str(uuid4())
    storage: dict[str, object] = {"session_token": token, "authenticated": True}
    ui = MagicMock()
    ui.context.client.request.cookies = {"session_token": token}
    ui.context.client.page.path = "/jobs"
    ui.context.client.page.response_timeout = _RESPONSE_TIMEOUT
    monkeypatch.setattr(middleware, "app", SimpleNamespace(storage=SimpleNamespace(user=storage)))
    monkeypatch.setattr(middleware, "ui", ui)
    await middleware.require_auth(page)()
    assert not ran
    assert storage == {"return_to": "/jobs"}
    ui.navigate.to.assert_called_once_with("/login")  # type: ignore[attr-defined]


async def test_an_unexpected_validation_error_is_not_treated_as_an_outage(
    visitor: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-connection error is not an outage and not a sign-out, so it
    reaches the caller instead of being dressed up as either."""
    ran = False
    stored = dict(visitor)

    async def page() -> None:
        nonlocal ran
        ran = True

    async def broken(_token: str) -> dict[str, object] | None:
        raise ValueError("unexpected")

    monkeypatch.setattr(middleware, "validate_session", broken)
    with pytest.raises(ValueError, match="unexpected"):
        await middleware.require_auth(page)()
    assert not ran
    # no "database unreachable" screen
    middleware.ui.button.assert_not_called()  # type: ignore[attr-defined]
    assert call(_OUTAGE) not in middleware.ui.label.call_args_list  # type: ignore[attr-defined]
    middleware.ui.navigate.to.assert_not_called()  # type: ignore[attr-defined]
    assert visitor == stored


async def _session_exists(test_engine: AsyncEngine, token: UUID) -> bool:
    async with test_engine.connect() as conn:
        return await conn.scalar(select(Session.id).where(Session.id == token)) is not None


async def test_logout_during_an_outage_signs_out_now_and_revokes_later(
    database: Forwarder,
    test_engine: AsyncEngine,
    session_token: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signing out clears the per-browser copy NiceGUI keeps, or the next
    page would still find the visitor signed in."""
    from openscientist.auth import fastapi_routes

    stored: dict[str, object] = {"user_id": str(session_token), "authenticated": True}
    monkeypatch.setattr(
        fastapi_routes, "app", SimpleNamespace(storage=SimpleNamespace(user=stored))
    )
    app = FastAPI()
    app.include_router(router)
    database.down()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"session_token": str(session_token)},
    ) as client:
        response = await client.post("/auth/logout", json={})
    assert response.headers["location"] == "/login"
    assert 'session_token=""' in response.headers["set-cookie"]
    assert await _session_exists(test_engine, session_token)
    assert stored == {}
    database.up()
    for _ in range(100):
        if not await _session_exists(test_engine, session_token):
            break
        await asyncio.sleep(0.2)
    assert not await _session_exists(test_engine, session_token)


async def test_sign_out_fails_loudly_when_the_session_delete_is_rejected(
    test_engine: AsyncEngine,
    session_token: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected delete is not an outage, so it must not read as a
    successful sign-out, and the session stays in place."""
    from openscientist.auth import fastapi_routes

    async def broken(_open_session: object, _token: str) -> None:
        raise ValueError("constraint violation")

    monkeypatch.setattr(fastapi_routes, "_delete_session", broken)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"session_token": str(session_token)},
    ) as client:
        with pytest.raises(ValueError, match="constraint violation"):
            await client.post("/auth/logout", json={})
    assert await _session_exists(test_engine, session_token)


async def test_sign_out_without_a_browser_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside a browser request there is no per-browser token to clear, and
    the sign-out still goes through."""

    class _Storage:
        @property
        def user(self) -> object:
            raise RuntimeError("no running task in app.storage.user")

    class _App:
        storage = _Storage()

    from openscientist.auth import fastapi_routes

    monkeypatch.setattr(fastapi_routes, "app", _App())
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/logout", json={})
    assert response.headers["location"] == "/login"
    assert 'session_token=""' in response.headers["set-cookie"]


async def test_a_cross_site_get_to_logout_changes_nothing(
    database: Forwarder,
    test_engine: AsyncEngine,
    session_token: UUID,
) -> None:
    """A top-level GET from another site can no longer sign a visitor out."""
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"session_token": str(session_token)},
    ) as client:
        response = await client.get("/auth/logout")
    assert response.status_code == 405
    assert "set-cookie" not in response.headers
    assert await _session_exists(test_engine, session_token)


async def test_a_failed_deferred_revoke_leaves_the_session_and_reports_it(
    test_engine: AsyncEngine,
    session_token: UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the deferred revoke dies on something an outage will not fix, the
    session stays live and the failure is reported to the operator."""
    from openscientist.auth import fastapi_routes

    async def broken(_open_session: object, _token: str) -> None:
        raise ValueError("constraint violation")

    monkeypatch.setattr(fastapi_routes, "_delete_session", broken)
    with caplog.at_level(logging.ERROR):
        fastapi_routes._revoke_session_later(str(session_token))
        for _ in range(100):
            if any(
                r.getMessage() == "Could not revoke a signed-out session" for r in caplog.records
            ):
                break
            await asyncio.sleep(0.05)
    [record] = [
        r for r in caplog.records if r.getMessage() == "Could not revoke a signed-out session"
    ]
    assert record.exc_info is not None and record.exc_info[0] is ValueError
    assert await _session_exists(test_engine, session_token)

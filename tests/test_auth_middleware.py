"""Tests for authentication middleware behavior."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openscientist.auth import middleware


def _make_fake_context(session_token: str | None = None):
    """Build fake NiceGUI app/ui objects for middleware tests."""
    storage = {"existing": "value"}
    cookies = {}
    if session_token is not None:
        cookies["session_token"] = session_token

    fake_app = SimpleNamespace(storage=SimpleNamespace(user=storage))
    fake_ui = SimpleNamespace(
        context=SimpleNamespace(
            client=SimpleNamespace(
                request=SimpleNamespace(cookies=cookies),
                page=SimpleNamespace(path="/new", response_timeout=3.0),
            ),
            slot=MagicMock(),
        ),
        navigate=SimpleNamespace(to=MagicMock()),
    )
    return fake_app, fake_ui, storage


async def test_require_auth_rejects_invalid_cookie_session(monkeypatch):
    """Protected pages must validate sessions against the database."""
    fake_app, fake_ui, storage = _make_fake_context(session_token="bad-token")
    monkeypatch.setattr(middleware, "app", fake_app)
    monkeypatch.setattr(middleware, "ui", fake_ui)

    async def _fake_validate_session(_token: str):
        return None

    monkeypatch.setattr(middleware, "validate_session", _fake_validate_session)

    called = {"value": False}

    async def protected_page():
        called["value"] = True

    wrapped = middleware.require_auth(protected_page)
    await wrapped()

    assert called["value"] is False
    fake_ui.navigate.to.assert_called_once_with("/login")
    assert storage.get("return_to") == "/new"


async def test_require_auth_accepts_valid_session_and_populates_storage(monkeypatch):
    """Protected pages should execute only after successful validation."""
    fake_app, fake_ui, storage = _make_fake_context(session_token="good-token")
    monkeypatch.setattr(middleware, "app", fake_app)
    monkeypatch.setattr(middleware, "ui", fake_ui)

    async def _fake_validate_session(_token: str):
        return {
            "user_id": "123e4567-e89b-12d3-a456-426614174000",
            "email": "valid@example.com",
            "name": "Valid User",
            "is_admin": True,
            "is_approved": False,
            "can_start_jobs": True,
        }

    monkeypatch.setattr(middleware, "validate_session", _fake_validate_session)

    called = {"value": False}

    async def protected_page():
        called["value"] = True
        return "ok"

    wrapped = middleware.require_auth(protected_page)
    result = await wrapped()

    assert result == "ok"
    assert called["value"] is True
    assert storage["authenticated"] is True
    assert storage["user_id"] == "123e4567-e89b-12d3-a456-426614174000"
    assert storage["is_admin"] is True
    assert storage["is_approved"] is False
    assert storage["can_start_jobs"] is True
    fake_ui.navigate.to.assert_not_called()


def test_require_auth_refuses_a_sync_page():
    """A sync page would check the session on the event loop, so the decorator refuses it."""

    def sync_page() -> None:
        pass

    with pytest.raises(TypeError, match="require_auth needs an async page"):
        middleware.require_auth(sync_page)


def _signed_in_ui(monkeypatch, response_timeout: float) -> MagicMock:
    """A NiceGUI stand-in for a visitor with a valid session, on a page that
    has ``response_timeout``."""
    ui = MagicMock()
    ui.context.client.request.cookies = {"session_token": "good-token"}
    ui.context.client.page.path = "/jobs"
    ui.context.client.page.response_timeout = response_timeout
    monkeypatch.setattr(middleware, "app", SimpleNamespace(storage=SimpleNamespace(user={})))
    monkeypatch.setattr(middleware, "ui", ui)

    async def _valid(_token: str):
        return {"user_id": "u", "email": "visitor@example.com", "name": "Visitor"}

    monkeypatch.setattr(middleware, "validate_session", _valid)
    return ui


async def test_a_cancelled_request_cancels_its_page_build(monkeypatch):
    """A request cancelled mid-build, as when the visitor leaves, takes its
    page build with it instead of leaving the build running."""
    _signed_in_ui(monkeypatch, response_timeout=30.0)
    building = asyncio.Event()
    build_cancelled = asyncio.Event()

    async def page():
        building.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            build_cancelled.set()
            raise

    request = asyncio.create_task(middleware.require_auth(page)())
    await building.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.wait_for(build_cancelled.wait(), timeout=5)


async def test_a_build_failing_after_its_deadline_is_logged(monkeypatch, caplog):
    """A build abandoned at its deadline whose cleanup then fails, as a driver
    closing a hung connection can, still shows the outage page in time and
    has its failure logged once it ends."""
    ui = _signed_in_ui(monkeypatch, response_timeout=0.6)
    release = asyncio.Event()

    async def page():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("closing the hung connection failed") from None

    with caplog.at_level(logging.DEBUG, logger=middleware.__name__):
        assert await asyncio.wait_for(middleware.require_auth(page)(), timeout=5) is None
        ui.label.assert_any_call("OpenScientist cannot reach its database right now.")
        release.set()
        for _ in range(100):
            if "closing the hung connection failed" in caplog.text:
                break
            await asyncio.sleep(0.01)
    assert "closing the hung connection failed" in caplog.text


def test_can_current_user_start_jobs_uses_approval_or_admin_fallback(monkeypatch):
    """Job-start capability should fall back to approval/admin flags."""
    fake_app = SimpleNamespace(
        storage=SimpleNamespace(user={"is_approved": False, "is_admin": True})
    )
    monkeypatch.setattr(middleware, "app", fake_app)

    assert middleware.can_current_user_start_jobs() is True

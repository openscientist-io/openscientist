"""Tests for authentication middleware behavior."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from open_scientist.auth import middleware


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
                page=SimpleNamespace(path="/new"),
            )
        ),
        navigate=SimpleNamespace(to=MagicMock()),
    )
    return fake_app, fake_ui, storage


def test_require_auth_sync_rejects_invalid_cookie_session(monkeypatch):
    """Sync-protected pages must validate sessions against the database."""
    fake_app, fake_ui, storage = _make_fake_context(session_token="bad-token")
    monkeypatch.setattr(middleware, "app", fake_app)
    monkeypatch.setattr(middleware, "ui", fake_ui)

    async def _fake_validate_session(_token: str):
        return None

    monkeypatch.setattr(middleware, "validate_session", _fake_validate_session)

    called = {"value": False}

    def protected_page():
        called["value"] = True

    wrapped = middleware.require_auth(protected_page)
    wrapped()

    assert called["value"] is False
    fake_ui.navigate.to.assert_called_once_with("/login")
    assert storage.get("return_to") == "/new"


def test_require_auth_sync_accepts_valid_session_and_populates_storage(monkeypatch):
    """Sync-protected pages should execute only after successful validation."""
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

    def protected_page():
        called["value"] = True
        return "ok"

    wrapped = middleware.require_auth(protected_page)
    result = wrapped()

    assert result == "ok"
    assert called["value"] is True
    assert storage["authenticated"] is True
    assert storage["user_id"] == "123e4567-e89b-12d3-a456-426614174000"
    assert storage["is_admin"] is True
    assert storage["is_approved"] is False
    assert storage["can_start_jobs"] is True
    fake_ui.navigate.to.assert_not_called()


def test_can_current_user_start_jobs_uses_approval_or_admin_fallback(monkeypatch):
    """Job-start capability should fall back to approval/admin flags."""
    fake_app = SimpleNamespace(
        storage=SimpleNamespace(user={"is_approved": False, "is_admin": True})
    )
    monkeypatch.setattr(middleware, "app", fake_app)

    assert middleware.can_current_user_start_jobs() is True

"""Regression coverage for session cookie security flags."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.responses import RedirectResponse

from openscientist.auth.fastapi_routes import _set_session_cookie


def _cookie_header(response: RedirectResponse) -> str:
    header = response.headers.get("set-cookie")
    assert header is not None
    return header


def test_set_session_cookie_httponly_and_samesite_lax() -> None:
    """Session cookies are HttpOnly with SameSite=Lax."""
    response = RedirectResponse(url="/", status_code=303)
    settings = MagicMock()
    settings.auth = SimpleNamespace(
        session_duration_days=7,
        app_url="http://localhost:8080",
    )

    with patch("openscientist.auth.fastapi_routes.get_settings", return_value=settings):
        _set_session_cookie(response, "session-id-http")

    cookie = _cookie_header(response)
    assert "session_token=session-id-http" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_set_session_cookie_secure_when_app_url_is_https() -> None:
    """Secure is set when APP_URL / auth.app_url uses HTTPS."""
    response = RedirectResponse(url="/", status_code=303)
    settings = MagicMock()
    settings.auth = SimpleNamespace(
        session_duration_days=14,
        app_url="https://openscientist.example.com",
    )

    with patch("openscientist.auth.fastapi_routes.get_settings", return_value=settings):
        _set_session_cookie(response, "session-id-https")

    cookie = _cookie_header(response)
    assert "session_token=session-id-https" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" in cookie


def test_set_session_cookie_not_secure_when_app_url_is_http() -> None:
    """Secure is omitted for non-HTTPS APP_URL values."""
    response = RedirectResponse(url="/", status_code=303)
    settings = MagicMock()
    settings.auth = SimpleNamespace(
        session_duration_days=7,
        app_url="http://localhost:8080",
    )

    with patch("openscientist.auth.fastapi_routes.get_settings", return_value=settings):
        _set_session_cookie(response, "session-id-insecure")

    cookie = _cookie_header(response)
    assert "Secure" not in cookie

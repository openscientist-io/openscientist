"""The login cookie is the web's only credential.

The copy of the signed-in user that NiceGUI keeps per browser, and the cookie
that carries its session id, must not sign anyone in on their own, and the
middleware that signs that cookie keeps it off plain HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.auth import middleware
from openscientist.database.models import Job, Session, User
from openscientist.settings import clear_settings_cache
from openscientist.webapp_components.artifact_routes import router as job_router
from openscientist.webapp_components.share_routes import router as share_router
from tests.db_outage import Forwarder

_PLOT = b"\x89PNG\r\n\x1a\ncookie-auth-plot"


@pytest_asyncio.fixture
async def visitor_login(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """A user and the id of their live database session, committed so the
    app's own admin login sees them."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(
            email=f"cookie-{uuid4().hex}@cookie.test",
            name="Cookie Visitor",
            is_approved=True,
        )
        db.add(user)
        await db.flush()
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield user, str(login.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


def _stored_browser(monkeypatch: pytest.MonkeyPatch, user: User, token: str) -> None:
    """The per-browser copy NiceGUI keeps for a signed-in browser."""
    storage: dict[str, object] = {
        "session_token": token,
        "user_id": str(user.id),
        "authenticated": True,
    }
    monkeypatch.setattr(middleware, "app", SimpleNamespace(storage=SimpleNamespace(user=storage)))


def _page_visitor(cookies: dict[str, str]) -> SimpleNamespace:
    """The NiceGUI context of a page request carrying ``cookies``."""
    ui = MagicMock()
    ui.context.client.request.cookies = cookies
    ui.context.client.page.path = "/jobs"
    ui.context.client.page.response_timeout = 5.0
    return ui


@pytest_asyncio.fixture
async def owned_job_with_plot(
    test_engine: AsyncEngine,
    visitor_login: tuple[User, str],
    tmp_path: Path,
) -> AsyncIterator[Job]:
    """The visitor's committed job and its plot on disk, so a real per-request
    session sees the job the way the web's does."""
    user, _ = visitor_login
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        job = Job(owner_id=user.id, research_question="Cookie auth", status="completed")
        db.add(job)
        await db.commit()
        await db.refresh(job)
    (tmp_path / str(job.id) / "provenance").mkdir(parents=True)
    (tmp_path / str(job.id) / "provenance" / "plot.png").write_bytes(_PLOT)
    yield job
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.commit()


def _web_app() -> FastAPI:
    """The /web routers on the app's real per-request sessions."""
    app = FastAPI()
    app.include_router(job_router)
    app.include_router(share_router)
    return app


def _jobs_manager(jobs_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(jobs_dir=jobs_dir)


@pytest.mark.asyncio
async def test_a_page_without_the_login_cookie_stays_signed_out_even_with_a_stored_token(
    database: Forwarder, visitor_login: tuple[User, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser's NiceGUI storage still holds a valid token, but without
    the login cookie the page must not run."""
    user, token = visitor_login
    _stored_browser(monkeypatch, user, token)
    monkeypatch.setattr(middleware, "ui", _page_visitor({"session": "browser-id"}))

    ran = False

    async def page() -> None:
        nonlocal ran
        ran = True

    await middleware.require_auth(page)()

    assert not ran
    middleware.ui.navigate.to.assert_called_once_with("/login")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_page_with_the_valid_login_cookie_still_reaches_the_page(
    database: Forwarder, visitor_login: tuple[User, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cookie alone signs the page in, and the token itself never enters
    the per-browser storage."""
    user, token = visitor_login
    storage: dict[str, object] = {}
    monkeypatch.setattr(middleware, "app", SimpleNamespace(storage=SimpleNamespace(user=storage)))
    monkeypatch.setattr(
        middleware, "ui", _page_visitor({"session": "browser-id", "session_token": token})
    )

    ran = False

    async def page() -> None:
        nonlocal ran
        ran = True

    await middleware.require_auth(page)()

    assert ran
    assert "session_token" not in storage
    assert storage["user_id"] == str(user.id)
    assert storage["authenticated"] is True
    middleware.ui.navigate.to.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_web_routes_are_not_signed_in_with_the_nicegui_cookie_alone(
    visitor_login: tuple[User, str],
    owned_job_with_plot: Job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The storage holds the signed-in user, but without the login cookie the
    job and share routes must refuse."""
    user, token = visitor_login
    job = owned_job_with_plot
    _stored_browser(monkeypatch, user, token)
    app = _web_app()
    with patch("openscientist.web_app.get_job_manager", return_value=_jobs_manager(tmp_path)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.cookies.set("session", "browser-id")
            plot = await c.get(f"/web/jobs/{job.id}/files/provenance/plot.png")
            shares = await c.get(f"/web/shares/job/{job.id}")
    assert plot.status_code == 401
    assert shares.status_code == 401


@pytest.mark.asyncio
async def test_an_expired_login_cookie_is_refused_by_the_web_routes(
    visitor_login: tuple[User, str],
    owned_job_with_plot: Job,
    test_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired session is signed out on the /web routes even when the
    browser's storage still holds the user."""
    user, _ = visitor_login
    job = owned_job_with_plot
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        expired = Session(user_id=user.id, expires_at=datetime.now(UTC) - timedelta(days=1))
        db.add(expired)
        await db.commit()
        token = str(expired.id)
    _stored_browser(monkeypatch, user, token)
    app = _web_app()
    with patch("openscientist.web_app.get_job_manager", return_value=_jobs_manager(tmp_path)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.cookies.set("session_token", token)
            plot = await c.get(f"/web/jobs/{job.id}/files/provenance/plot.png")
            shares = await c.get(f"/web/shares/job/{job.id}")
    assert plot.status_code == 401
    assert shares.status_code == 401


@pytest.mark.asyncio
async def test_a_deleted_login_cookie_is_refused_by_the_web_routes(
    visitor_login: tuple[User, str],
    owned_job_with_plot: Job,
    test_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed-out session's cookie is signed out on the /web routes even
    when the browser's storage still holds the user."""
    user, _ = visitor_login
    job = owned_job_with_plot
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        signed_out = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(signed_out)
        await db.commit()
        token = str(signed_out.id)
        await db.delete(signed_out)
        await db.commit()
    _stored_browser(monkeypatch, user, token)
    app = _web_app()
    with patch("openscientist.web_app.get_job_manager", return_value=_jobs_manager(tmp_path)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.cookies.set("session_token", token)
            plot = await c.get(f"/web/jobs/{job.id}/files/provenance/plot.png")
            shares = await c.get(f"/web/shares/job/{job.id}")
    assert plot.status_code == 401
    assert shares.status_code == 401


@pytest.mark.asyncio
async def test_a_valid_login_cookie_still_signs_the_web_routes_in(
    visitor_login: tuple[User, str], owned_job_with_plot: Job, tmp_path: Path
) -> None:
    """The cookie alone signs the /web routes in, with no per-browser storage
    involved."""
    user, token = visitor_login
    job = owned_job_with_plot
    app = _web_app()
    with patch("openscientist.web_app.get_job_manager", return_value=_jobs_manager(tmp_path)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.cookies.set("session_token", token)
            plot = await c.get(f"/web/jobs/{job.id}/files/provenance/plot.png")
            shares = await c.get(f"/web/shares/job/{job.id}")
    assert plot.status_code == 200
    assert plot.content == _PLOT
    assert shares.status_code == 200
    assert shares.json() == []


def _session_cookie_flags(
    monkeypatch: pytest.MonkeyPatch, app_url: str, tmp_path: Path
) -> dict[str, object]:
    """The session middleware flags ``create_app`` hands to ``run_with`` for
    ``APP_URL``=``app_url``.

    ``run_with`` is intercepted, not called for real. The real one mutates
    NiceGUI's single global app, and once any earlier test has run a request
    through it, its middleware stack is already built and the call raises.
    """
    monkeypatch.setenv("APP_URL", app_url)
    clear_settings_cache()
    monkeypatch.setattr(web_app, "_state", web_app._AppState())
    monkeypatch.setattr(web_app, "_register_openapi_docs", _noop)
    monkeypatch.setattr(web_app, "_register_health_endpoint", _noop)
    monkeypatch.setattr(web_app, "_register_robots_txt", _noop)
    monkeypatch.setattr(web_app, "_register_api_routes", _noop)
    monkeypatch.setattr(web_app, "_register_oauth_routes", _noop)
    monkeypatch.setattr(web_app, "_register_share_routes", _noop)
    monkeypatch.setattr(web_app, "_initialize_job_manager_runtime", _noop)
    monkeypatch.setattr(web_app, "_register_nicegui_static_files", _noop)
    monkeypatch.setattr(web_app, "_register_pwa_metadata", _noop)
    real = web_app.importlib.import_module
    monkeypatch.setattr(
        web_app.importlib,
        "import_module",
        lambda name, package=None: (
            None if name == "openscientist.webapp_components.pages" else real(name, package)
        ),
    )
    captured: dict[str, dict[str, object]] = {}

    def _capture_run_with(_host_app: FastAPI, **kwargs: object) -> None:
        captured["session"] = cast(dict[str, object], kwargs["session_middleware_kwargs"])

    monkeypatch.setattr(web_app.ui, "run_with", _capture_run_with)
    web_app.create_app(tmp_path / "jobs")
    return captured["session"]


def _noop(*_args: object, **_kwargs: object) -> None:
    pass


def test_the_session_cookie_is_https_only_for_an_https_app_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behind https the app's session cookie must never travel over plain
    HTTP."""
    flags = _session_cookie_flags(monkeypatch, "https://os.example", tmp_path)
    assert flags.get("https_only") is True
    assert flags.get("same_site") == "lax"


def test_the_session_cookie_stays_plain_for_an_http_app_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An http APP_URL serves a plaintext staging, so Secure must stay off or
    the interface breaks there."""
    flags = _session_cookie_flags(monkeypatch, "http://os.local:8080", tmp_path)
    assert flags.get("https_only") is False
    assert flags.get("same_site") == "lax"

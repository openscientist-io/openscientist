"""Tests for SlowAPI rate limiting on API and auth endpoints."""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.api.auth import get_current_user_from_api_key
from openscientist.api.rate_limits import (
    AUTH_RATE_LIMIT,
    HEALTH_RATE_LIMIT,
    MUTATING_RATE_LIMIT,
    configure_host_rate_limiting,
    limiter,
    wire_rate_limiter,
)
from openscientist.api.router import api_router
from openscientist.auth.fastapi_routes import router as auth_router
from openscientist.database.models import User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session


@pytest_asyncio.fixture
async def test_user_db(db_session: AsyncSession) -> User:
    user = User(
        email="ratelimit@example.com",
        name="Rate Limit Test User",
        is_approved=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    """Clear in-memory rate limit counters between tests."""
    limiter.reset()


def _build_api_app(db_session: AsyncSession, user: User) -> FastAPI:
    app = FastAPI()
    configure_host_rate_limiting(app)
    app.include_router(api_router)

    async def override_get_session():
        await set_current_user(db_session, user.id)
        yield db_session

    async def override_get_user():
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_from_api_key] = override_get_user
    return app


def _build_auth_app() -> FastAPI:
    app = FastAPI()
    configure_host_rate_limiting(app)
    app.include_router(auth_router)
    return app


class TestHealthRateLimit:
    """Health endpoint rate limiting after middleware wiring."""

    @pytest.mark.asyncio
    async def test_health_succeeds_below_threshold(self) -> None:
        app = FastAPI()
        configure_host_rate_limiting(app)
        app.include_router(api_router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for _ in range(3):
                response = await client.get("/api/v1/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_returns_429_after_limit(self) -> None:
        app = FastAPI()
        configure_host_rate_limiting(app)
        app.include_router(api_router)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            statuses = [await client.get("/api/v1/health") for _ in range(11)]
            status_codes = [response.status_code for response in statuses]

        assert all(code == 200 for code in status_codes[:10])
        assert status_codes[10] == 429


class TestAuthRateLimit:
    """Authentication endpoint rate limiting."""

    @pytest.mark.asyncio
    async def test_mock_login_returns_429_after_limit(self, db_session: AsyncSession) -> None:
        app = _build_auth_app()
        mock_session = AsyncMock()
        mock_session.id = uuid.uuid4()
        mock_user = MagicMock()
        mock_user.id = uuid.uuid4()
        mock_user.email = "dev@example.com"

        @asynccontextmanager
        async def mock_admin_session():
            yield db_session

        with (
            patch(
                "openscientist.auth.fastapi_routes.get_settings",
                return_value=MagicMock(dev=MagicMock(dev_mode=True)),
            ),
            patch(
                "openscientist.auth.fastapi_routes.get_admin_session",
                mock_admin_session,
            ),
            patch(
                "openscientist.auth.fastapi_routes.create_or_update_user",
                new=AsyncMock(return_value=mock_user),
            ),
            patch(
                "openscientist.auth.fastapi_routes.create_session",
                new=AsyncMock(return_value=mock_session),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                statuses = [await client.get("/auth/mock/login") for _ in range(11)]
                status_codes = [response.status_code for response in statuses]

        assert all(code == 303 for code in status_codes[:10])
        assert status_codes[10] == 429


class TestMutatingApiRateLimit:
    """Mutating REST API endpoint rate limiting."""

    @pytest.mark.asyncio
    async def test_cancel_job_returns_429_after_limit(
        self,
        db_session: AsyncSession,
        test_user_db: User,
    ) -> None:
        app = _build_api_app(db_session, test_user_db)
        missing_job_id = uuid.uuid4()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            status_codes = [
                (await client.post(f"/api/v1/jobs/{missing_job_id}/cancel")).status_code
                for _ in range(31)
            ]

        assert status_codes[29] == 404
        assert status_codes[30] == 429


class TestRateLimitConstants:
    """Sanity checks for shared limit strings."""

    def test_limit_constants(self) -> None:
        assert HEALTH_RATE_LIMIT == "10/minute"
        assert AUTH_RATE_LIMIT == "10/minute"
        assert MUTATING_RATE_LIMIT == "30/minute"

    def test_nicegui_app_gets_limiter_state(self) -> None:
        from nicegui import app as nicegui_app

        wire_rate_limiter(nicegui_app)
        assert nicegui_app.state.limiter is limiter


class TestMountedNiceGuiRateLimit:
    """Rate limiting through host SlowAPIMiddleware with mounted NiceGUI routes."""

    @pytest.mark.asyncio
    async def test_mock_login_rate_limited_on_mounted_host_app(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nicegui import ui
        from slowapi.middleware import SlowAPIMiddleware

        from openscientist import web_app

        monkeypatch.setattr(web_app, "_state", web_app._AppState())

        host_app = FastAPI()
        configure_host_rate_limiting(host_app)
        wire_rate_limiter(web_app.app)
        web_app._register_oauth_routes()
        ui.run_with(host_app, mount_path="/", storage_secret="test-rate-limit-secret")

        assert SlowAPIMiddleware in [middleware.cls for middleware in host_app.user_middleware]

        mock_session = AsyncMock()
        mock_session.id = uuid.uuid4()
        mock_user = MagicMock()
        mock_user.id = uuid.uuid4()
        mock_user.email = "dev@example.com"

        @asynccontextmanager
        async def mock_admin_session():
            yield db_session

        with (
            patch(
                "openscientist.auth.fastapi_routes.get_settings",
                return_value=MagicMock(dev=MagicMock(dev_mode=True)),
            ),
            patch(
                "openscientist.auth.fastapi_routes.get_admin_session",
                mock_admin_session,
            ),
            patch(
                "openscientist.auth.fastapi_routes.create_or_update_user",
                new=AsyncMock(return_value=mock_user),
            ),
            patch(
                "openscientist.auth.fastapi_routes.create_session",
                new=AsyncMock(return_value=mock_session),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=host_app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                status_codes = [
                    (await client.get("/auth/mock/login")).status_code for _ in range(11)
                ]

        assert all(code == 303 for code in status_codes[:10])
        assert status_codes[10] == 429

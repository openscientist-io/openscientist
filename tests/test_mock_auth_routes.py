"""Integration tests for mock OAuth routes when development mode is disabled."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from openscientist.api.rate_limits import configure_host_rate_limiting
from openscientist.auth.fastapi_routes import router as auth_router


def _build_auth_app() -> FastAPI:
    app = FastAPI()
    configure_host_rate_limiting(app)
    app.include_router(auth_router)
    return app


class TestMockAuthDisabledWhenNotDevMode:
    """Mock login/admin/callback must 404 when ``dev_mode`` is False."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/auth/mock/login"),
            ("GET", "/auth/mock/admin-login"),
            ("POST", "/auth/mock/callback"),
        ],
    )
    async def test_mock_auth_routes_return_404_when_dev_mode_disabled(
        self,
        method: str,
        path: str,
    ) -> None:
        """Disabled mock auth returns 404 and never establishes a session cookie."""
        app = _build_auth_app()

        with (
            patch(
                "openscientist.auth.fastapi_routes.get_settings",
                return_value=MagicMock(dev=MagicMock(dev_mode=False)),
            ),
            patch(
                "openscientist.auth.fastapi_routes.create_or_update_user",
                new_callable=AsyncMock,
            ) as mock_create_user,
            patch(
                "openscientist.auth.fastapi_routes.create_session",
                new_callable=AsyncMock,
            ) as mock_create_session,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                if method == "GET":
                    response = await client.get(path)
                else:
                    response = await client.post(
                        path,
                        data={
                            "email": "dev@example.com",
                            "name": "Dev User",
                            "username": "devuser",
                        },
                    )

        assert response.status_code == 404
        assert response.json()["detail"] == "Mock auth not enabled"
        assert "session_token" not in response.cookies
        mock_create_user.assert_not_called()
        mock_create_session.assert_not_called()

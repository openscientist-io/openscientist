"""Tests for web-session share routes."""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.middleware import get_current_user_id
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.webapp_components.share_routes import router
from tests.helpers import enable_rls


@pytest.mark.asyncio
async def test_web_share_create_uses_admin_lookup_for_target_user(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """
    Creating a web share should resolve target users via admin lookup.

    Under RLS, a regular app session cannot query arbitrary users by email.
    """
    job = Job(
        owner_id=test_user.id,
        title="Web Shared Job",
        description="share route test",
        status="pending",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    await enable_rls(db_session)

    app = FastAPI()

    async def override_get_session():
        await set_current_user(db_session, test_user.id)
        yield db_session

    async def override_get_current_user_id():
        return test_user.id

    @asynccontextmanager
    async def mock_get_admin_session():
        # Temporarily elevate the same session for cross-user lookup.
        await db_session.execute(text("SET ROLE openscientist_admin"))
        try:
            yield db_session
        finally:
            await db_session.execute(text("SET ROLE openscientist_app"))
            await set_current_user(db_session, test_user.id)

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_id] = override_get_current_user_id
    app.include_router(router)

    with patch(
        "openscientist.webapp_components.share_routes.get_admin_session", mock_get_admin_session
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/web/shares/job/{job.id}",
                json={
                    "shared_with_email": test_user2.email,
                    "permission_level": "view",
                },
            )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["job_id"] == str(job.id)
    assert data["shared_with_email"] == test_user2.email
    assert data["permission_level"] == "view"


@pytest.mark.asyncio
async def test_web_share_create_rejects_inactive_target_user(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Web share creation should reject inactive target users."""
    job = Job(
        owner_id=test_user.id,
        title="Web Shared Job Inactive Target",
        description="share route inactive user test",
        status="pending",
    )
    db_session.add(job)
    test_user2.is_active = False
    await db_session.commit()
    await db_session.refresh(job)

    await enable_rls(db_session)

    app = FastAPI()

    async def override_get_session():
        await set_current_user(db_session, test_user.id)
        yield db_session

    async def override_get_current_user_id():
        return test_user.id

    @asynccontextmanager
    async def mock_get_admin_session():
        await db_session.execute(text("SET ROLE openscientist_admin"))
        try:
            yield db_session
        finally:
            await db_session.execute(text("SET ROLE openscientist_app"))
            await set_current_user(db_session, test_user.id)

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_id] = override_get_current_user_id
    app.include_router(router)

    with patch(
        "openscientist.webapp_components.share_routes.get_admin_session", mock_get_admin_session
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/web/shares/job/{job.id}",
                json={
                    "shared_with_email": test_user2.email,
                    "permission_level": "view",
                },
            )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()

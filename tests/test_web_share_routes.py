"""Tests for web-session share routes."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job, Session, User
from openscientist.webapp_components.share_routes import router


@pytest_asyncio.fixture
async def signed_in_owner(
    test_engine: AsyncEngine,
) -> AsyncIterator[tuple[User, str, Job]]:
    """A committed owner, the id of their live session (which the route's own
    admin login must see) and a job they own, so the route's real per-request
    sessions read the same rows the web's do."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"share-owner-{uuid4().hex}@share.test", name="Share Owner")
        db.add(owner)
        await db.flush()
        login, login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(login)
        job = Job(
            owner_id=owner.id,
            research_question="Web Shared Job",
            description="share route test",
            status="pending",
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
    yield owner, login_token, job
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest_asyncio.fixture
async def share_target(test_engine: AsyncEngine) -> AsyncIterator[User]:
    """A committed active user to share with, whose row the owner's RLS
    session cannot see by email."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        target = User(email=f"share-target-{uuid4().hex}@share.test", name="Share Target")
        db.add(target)
        await db.commit()
        await db.refresh(target)
    yield target
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == target.id))
        await db.commit()


async def _set_target_active(test_engine: AsyncEngine, target: User, active: bool) -> None:
    """Flip the target's active flag, committed, so the admin lookup sees it."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        row = (await db.execute(select(User).where(User.id == target.id))).scalar_one()
        row.is_active = active
        await db.commit()


async def _set_owner_approved(test_engine: AsyncEngine, owner: User) -> None:
    """Approve the caller, committed, so the route's real admin login sees it."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        row = (await db.execute(select(User).where(User.id == owner.id))).scalar_one()
        row.is_approved = True
        await db.commit()


def _app() -> FastAPI:
    """The share router on the app's real per-request sessions."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_web_share_create_uses_admin_lookup_for_target_user(
    share_target: User,
    signed_in_owner: tuple[User, str, Job],
) -> None:
    """
    Creating a web share should resolve target users via admin lookup.

    Under RLS, a regular app session cannot query arbitrary users by email.
    """
    _, token, job = signed_in_owner

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            f"/web/shares/job/{job.id}",
            json={
                "shared_with_email": share_target.email,
                "permission_level": "view",
            },
            headers={"cookie": f"session_token={token}"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["job_id"] == str(job.id)
    assert data["shared_with_email"] == share_target.email
    assert data["permission_level"] == "view"


@pytest.mark.asyncio
async def test_web_share_create_rejects_inactive_target_user(
    share_target: User,
    signed_in_owner: tuple[User, str, Job],
    test_engine: AsyncEngine,
) -> None:
    """Web share creation should reject inactive target users."""
    _, token, job = signed_in_owner
    await _set_target_active(test_engine, share_target, False)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            f"/web/shares/job/{job.id}",
            json={
                "shared_with_email": share_target.email,
                "permission_level": "view",
            },
            headers={"cookie": f"session_token={token}"},
        )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_web_share_listing_shows_the_share_the_owner_just_created(
    share_target: User,
    signed_in_owner: tuple[User, str, Job],
) -> None:
    """The owner's listing shows their shares, with the shared-with users
    resolved the way the admin lookup does, not from the owner's own RLS rows."""
    _, token, job = signed_in_owner

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        headers = {"cookie": f"session_token={token}"}
        created = await client.post(
            f"/web/shares/job/{job.id}",
            json={
                "shared_with_email": share_target.email,
                "permission_level": "edit",
            },
            headers=headers,
        )
        listing = await client.get(f"/web/shares/job/{job.id}", headers=headers)

    assert created.status_code == 200, created.text
    assert listing.status_code == 200, listing.text
    entries = listing.json()
    assert [entry["shared_with_email"] for entry in entries] == [share_target.email]
    assert entries[0]["shared_with_name"] == share_target.name
    assert entries[0]["permission_level"] == "edit"


@pytest.mark.asyncio
async def test_web_lookup_refuses_unapproved_caller(
    share_target: User,
    signed_in_owner: tuple[User, str, Job],
) -> None:
    """A signed-in account without approval cannot look up share targets."""
    _, token, _job = signed_in_owner

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(
            "/web/shares/lookup",
            params={"email": share_target.email},
            headers={"cookie": f"session_token={token}"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_web_lookup_returns_exact_email_match_for_approved_caller(
    share_target: User,
    signed_in_owner: tuple[User, str, Job],
    test_engine: AsyncEngine,
) -> None:
    """An approved caller gets the one user for an exact email, id and name only."""
    owner, token, _job = signed_in_owner
    await _set_owner_approved(test_engine, owner)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(
            "/web/shares/lookup",
            params={"email": f" {share_target.email.upper()} "},
            headers={"cookie": f"session_token={token}"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"target": {"id": str(share_target.id), "name": share_target.name}}


@pytest.mark.asyncio
async def test_web_lookup_wildcards_and_missing_email_return_empty(
    share_target: User,
    signed_in_owner: tuple[User, str, Job],
    test_engine: AsyncEngine,
) -> None:
    """Wildcards, partial strings and unknown emails all return the same empty result."""
    owner, token, _job = signed_in_owner
    await _set_owner_approved(test_engine, owner)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        for email in (share_target.email[:5], "%%", "_", "nobody@example.invalid"):
            response = await client.get(
                "/web/shares/lookup",
                params={"email": email},
                headers={"cookie": f"session_token={token}"},
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"target": None}

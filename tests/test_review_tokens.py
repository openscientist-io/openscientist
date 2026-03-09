"""Tests for review token redemption routes."""

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.fastapi_routes import (
    _hash_token,
    generate_review_token,
    redeem_review_token,
)
from openscientist.database.models import ReviewToken, User


def _make_app() -> FastAPI:
    """Create a minimal FastAPI app with the review route."""
    test_app = FastAPI()
    test_app.add_api_route("/review/{token}", redeem_review_token, methods=["GET"])
    return test_app


def _mock_admin_session(db_session: AsyncSession):
    """Build a mock get_admin_session that yields the test db session."""

    @asynccontextmanager
    async def _ctx():
        yield db_session

    return _ctx


async def _create_review_token(
    db_session: AsyncSession,
    admin_user: User,
    *,
    label: str = "Reviewer 1",
    expires_at: datetime | None = None,
    is_active: bool = True,
) -> tuple[str, ReviewToken]:
    """Create a review token and return (plaintext, db_record)."""
    plaintext = generate_review_token()
    token = ReviewToken(
        token_hash=_hash_token(plaintext),
        label=label,
        created_by_id=admin_user.id,
        expires_at=expires_at,
        is_active=is_active,
    )
    db_session.add(token)
    await db_session.commit()
    await db_session.refresh(token)
    return plaintext, token


@pytest.mark.asyncio
async def test_redeem_review_token(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """First redemption creates an anonymous user, sets cookie, redirects to /."""
    plaintext, token = await _create_review_token(db_session, test_admin_user)
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.get(f"/review/{plaintext}")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "session_token" in response.cookies

    # Verify token is marked redeemed
    await db_session.refresh(token)
    assert token.redeemed_at is not None
    assert token.redeemed_by_id is not None


@pytest.mark.asyncio
async def test_redeem_sets_redeemed_by(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """redeemed_by_id points to the created anonymous user."""
    plaintext, token = await _create_review_token(db_session, test_admin_user)
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            await client.get(f"/review/{plaintext}")

    await db_session.refresh(token)
    assert token.redeemed_by_id is not None

    # Verify the user exists and has the right name
    from sqlalchemy import select

    result = await db_session.execute(select(User).where(User.id == token.redeemed_by_id))
    user = result.scalar_one()
    assert user.name == "Reviewer 1"


@pytest.mark.asyncio
async def test_re_redeem_logs_into_same_user(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Second click reuses the same anonymous user, creates new session."""
    plaintext, token = await _create_review_token(db_session, test_admin_user)
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp1 = await client.get(f"/review/{plaintext}")
            cookie1 = resp1.cookies.get("session_token")

            resp2 = await client.get(f"/review/{plaintext}")
            cookie2 = resp2.cookies.get("session_token")

    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/"
    assert cookie1 is not None
    assert cookie2 is not None
    # Different sessions
    assert cookie1 != cookie2

    # Same user
    await db_session.refresh(token)
    assert token.redeemed_by_id is not None


@pytest.mark.asyncio
async def test_redeem_expired_token(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Expired token redirects to login with error."""
    plaintext, _ = await _create_review_token(
        db_session,
        test_admin_user,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.get(f"/review/{plaintext}")

    assert response.status_code == 303
    assert "token_invalid" in response.headers["location"]


@pytest.mark.asyncio
async def test_redeem_revoked_token(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Revoked token redirects to login with error."""
    plaintext, _ = await _create_review_token(
        db_session,
        test_admin_user,
        is_active=False,
    )
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.get(f"/review/{plaintext}")

    assert response.status_code == 303
    assert "token_invalid" in response.headers["location"]


@pytest.mark.asyncio
async def test_redeem_invalid_token(
    db_session: AsyncSession,
):
    """Unknown token redirects to login with error."""
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.get("/review/nonexistent-token")

    assert response.status_code == 303
    assert "token_invalid" in response.headers["location"]


@pytest.mark.asyncio
async def test_reviewer_is_approved(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Anonymous user created with is_approved=True."""
    plaintext, token = await _create_review_token(db_session, test_admin_user)
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            await client.get(f"/review/{plaintext}")

    await db_session.refresh(token)
    from sqlalchemy import select

    result = await db_session.execute(select(User).where(User.id == token.redeemed_by_id))
    user = result.scalar_one()
    assert user.is_approved is True


@pytest.mark.asyncio
async def test_anonymous_email_format(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Email matches reviewer-{hex}@review.local pattern."""
    plaintext, token = await _create_review_token(db_session, test_admin_user)
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            await client.get(f"/review/{plaintext}")

    await db_session.refresh(token)
    from sqlalchemy import select

    result = await db_session.execute(select(User).where(User.id == token.redeemed_by_id))
    user = result.scalar_one()
    assert re.match(r"^reviewer-[0-9a-f]+@review\.local$", user.email)


@pytest.mark.asyncio
async def test_multiple_tokens_create_distinct_users(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Two different tokens create two different anonymous users."""
    plaintext1, token1 = await _create_review_token(db_session, test_admin_user, label="Reviewer A")
    plaintext2, token2 = await _create_review_token(db_session, test_admin_user, label="Reviewer B")
    app = _make_app()

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            await client.get(f"/review/{plaintext1}")
            await client.get(f"/review/{plaintext2}")

    await db_session.refresh(token1)
    await db_session.refresh(token2)
    assert token1.redeemed_by_id is not None
    assert token2.redeemed_by_id is not None
    assert token1.redeemed_by_id != token2.redeemed_by_id


@pytest.mark.asyncio
async def test_revoke_review_token(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Setting is_active=False revokes the token."""
    _, token = await _create_review_token(db_session, test_admin_user)
    assert token.is_active is True
    assert token.status == "active"

    token.is_active = False
    await db_session.commit()
    await db_session.refresh(token)

    assert token.is_active is False
    assert token.status == "revoked"

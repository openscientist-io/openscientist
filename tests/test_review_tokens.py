"""Tests for review token redemption routes."""

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.fastapi_routes import (
    _hash_token,
    generate_review_token,
    redeem_review_token,
    review_confirmation_page,
)
from openscientist.database.models import ReviewToken, Session, User


def _make_app() -> FastAPI:
    """Create a minimal FastAPI app with the review routes."""
    test_app = FastAPI()
    test_app.add_api_route("/review/{token}", review_confirmation_page, methods=["GET"])
    test_app.add_api_route("/review/{token}", redeem_review_token, methods=["POST"])
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
            response = await client.post(f"/review/{plaintext}", json={})

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs"
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
            await client.post(f"/review/{plaintext}", json={})

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
            resp1 = await client.post(f"/review/{plaintext}", json={})
            cookie1 = resp1.cookies.get("session_token")

            resp2 = await client.post(f"/review/{plaintext}", json={})
            cookie2 = resp2.cookies.get("session_token")

    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/jobs"
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
            response = await client.post(f"/review/{plaintext}", json={})

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
            response = await client.post(f"/review/{plaintext}", json={})

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
            response = await client.post("/review/nonexistent-token", json={})

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
            await client.post(f"/review/{plaintext}", json={})

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
            await client.post(f"/review/{plaintext}", json={})

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
            await client.post(f"/review/{plaintext1}", json={})
            await client.post(f"/review/{plaintext2}", json={})

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


async def _reviewer_user_count(db_session: AsyncSession) -> int:
    from sqlalchemy import select

    result = await db_session.execute(
        select(User).where(User.email.like("reviewer-%@review.local"))
    )
    return len(result.scalars().all())


async def _reviewer_sessions(db_session: AsyncSession, reviewer_id: UUID) -> list[UUID]:
    from sqlalchemy import select

    result = await db_session.execute(select(Session.id).where(Session.user_id == reviewer_id))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_get_link_shows_confirmation_without_signing_in(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """A top-level GET to the review link only shows the sign-in page."""
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

    assert response.status_code == 200
    assert "Reviewer 1" in response.text
    assert "session_token" not in response.cookies
    await db_session.refresh(token)
    assert token.redeemed_at is None
    assert await _reviewer_user_count(db_session) == 0


@pytest.mark.asyncio
async def test_get_link_refuses_revoked_token(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """A revoked link is refused on the confirmation page too."""
    plaintext, _ = await _create_review_token(db_session, test_admin_user, is_active=False)
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
async def test_form_encoded_sign_in_is_refused(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """A cross-site form can only post urlencoded, which the sign-in refuses."""
    plaintext, _ = await _create_review_token(db_session, test_admin_user)
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
            response = await client.post(f"/review/{plaintext}", data={"csrf": "no"})

    assert response.status_code == 422
    assert await _reviewer_user_count(db_session) == 0


@pytest.mark.asyncio
async def test_redeem_refuses_a_token_whose_reviewer_is_gone(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """A redeemed token left without a reviewer must not mint a new account."""
    plaintext, token = await _create_review_token(db_session, test_admin_user)
    token.redeemed_at = datetime.now(UTC)
    token.redeemed_by_id = None
    await db_session.commit()
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
            response = await client.post(f"/review/{plaintext}", json={})

    assert response.status_code == 303
    assert "token_invalid" in response.headers["location"]
    assert await _reviewer_user_count(db_session) == 0


@pytest.mark.asyncio
async def test_revoke_a_redeemed_token_ends_the_reviewer_session(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Revoking a redeemed token ends the reviewer's live session and the next use."""
    from openscientist.webapp_components.pages.admin import revoke_review_token

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
            response = await client.post(f"/review/{plaintext}", json={})
    assert response.status_code == 303

    await db_session.refresh(token)
    reviewer_id = token.redeemed_by_id
    assert reviewer_id is not None
    assert await _reviewer_sessions(db_session, reviewer_id) != []

    with patch(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        _mock_admin_session(db_session),
    ):
        success, _ = await revoke_review_token(token.id)
    assert success is True

    await db_session.refresh(token)
    assert token.is_active is False
    assert await _reviewer_sessions(db_session, reviewer_id) == []

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.post(f"/review/{plaintext}", json={})
    assert response.status_code == 303
    assert "token_invalid" in response.headers["location"]


@pytest.mark.asyncio
async def test_deleting_the_reviewer_refuses_the_next_use(
    db_session: AsyncSession,
    test_admin_user: User,
):
    """Deleting the redeemed reviewer revokes the token and creates no account."""
    from sqlalchemy import select

    from openscientist.webapp_components.pages.admin import delete_user

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
            response = await client.post(f"/review/{plaintext}", json={})
    assert response.status_code == 303

    await db_session.refresh(token)
    reviewer_id = token.redeemed_by_id
    assert reviewer_id is not None

    with patch(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        _mock_admin_session(db_session),
    ):
        success, _ = await delete_user(reviewer_id)
    assert success is True

    with patch(
        "openscientist.auth.fastapi_routes.get_admin_session",
        _mock_admin_session(db_session),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.post(f"/review/{plaintext}", json={})
    assert response.status_code == 303
    assert "token_invalid" in response.headers["location"]

    result = await db_session.execute(select(User).where(User.id == reviewer_id))
    assert result.scalar_one_or_none() is None
    assert await _reviewer_user_count(db_session) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", ["revoked", "expired", "creator_deleted"])
async def test_review_cutoff_refuses_existing_credentials(
    db_session: AsyncSession, test_admin_user: User, cutoff: str
) -> None:
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from sqlalchemy import delete

    from openscientist.api.auth import get_current_user_from_api_key, hash_secret
    from openscientist.auth.middleware import validate_session
    from openscientist.database.models import Administrator, APIKey
    from openscientist.webapp_components.pages.admin import delete_user, revoke_review_token

    plaintext, token = await _create_review_token(db_session, test_admin_user)
    session_context = _mock_admin_session(db_session)
    with (
        patch("openscientist.auth.fastapi_routes.get_admin_session", session_context),
        patch("openscientist.auth.middleware.get_admin_session", session_context),
        patch("openscientist.api.auth.get_admin_session", session_context),
        patch("openscientist.webapp_components.pages.admin.get_admin_session", session_context),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=_make_app()), base_url="http://test"
        ) as client:
            response = await client.post(f"/review/{plaintext}", json={})
        cookie = response.cookies["session_token"]
        await db_session.refresh(token)
        db_session.add(
            APIKey(user_id=token.redeemed_by_id, name="review", key_hash=hash_secret("secret"))
        )
        await db_session.commit()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="review:secret")
        assert (await get_current_user_from_api_key(credentials)).id == token.redeemed_by_id
        assert await validate_session(cookie) is not None
        if cutoff == "revoked":
            assert (await revoke_review_token(token.id))[0]
        elif cutoff == "creator_deleted":
            await db_session.execute(
                delete(Administrator).where(Administrator.user_id == test_admin_user.id)
            )
            await db_session.commit()
            assert (await delete_user(test_admin_user.id))[0]
        else:
            token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db_session.commit()
        with pytest.raises(HTTPException) as refused:
            await get_current_user_from_api_key(credentials)
        assert refused.value.status_code == 401
        assert await validate_session(cookie) is None

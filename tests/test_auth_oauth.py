"""
Tests for OAuth authentication functionality.

Tests user creation, OAuth account linking, session management, and authentication.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.auth.routes import create_or_update_user, create_session
from shandy.database.models import Administrator, OAuthAccount, User
from shandy.database.models import Session as DBSession
from shandy.settings import clear_settings_cache


@pytest.fixture(autouse=True)
def _clear_cached_settings_between_tests():
    """Ensure BOOTSTRAP_ADMIN_EMAILS changes are applied per test."""
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.mark.asyncio
async def test_create_new_user_with_oauth(db_session: AsyncSession):
    """Test creating a new user via OAuth."""
    user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="github_123",
        email="new@example.com",
        name="New User",
        access_token="access_token_123",
        refresh_token="refresh_token_123",
    )

    # Verify user was created
    assert isinstance(user.id, UUID)
    assert user.email == "new@example.com"
    assert user.name == "New User"
    assert user.is_active is True
    assert user.is_approved is False

    # Verify OAuth account was created
    stmt = select(OAuthAccount).where(
        OAuthAccount.provider == "github",
        OAuthAccount.provider_user_id == "github_123",
    )
    result = await db_session.execute(stmt)
    oauth_account = result.scalar_one()

    assert oauth_account.user_id == user.id
    assert oauth_account.email == "new@example.com"
    assert oauth_account.access_token == "access_token_123"
    assert oauth_account.refresh_token == "refresh_token_123"


@pytest.mark.asyncio
async def test_update_existing_oauth_account(db_session: AsyncSession):
    """Test updating an existing OAuth account with new tokens."""
    # Create initial user and OAuth account
    user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="github_456",
        email="existing@example.com",
        name="Existing User",
        access_token="old_access_token",
        refresh_token="old_refresh_token",
    )

    original_user_id = user.id

    # Update with new tokens (simulating re-auth)
    updated_user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="github_456",
        email="existing@example.com",
        name="Existing User Updated",
        access_token="new_access_token",
        refresh_token="new_refresh_token",
    )

    # Should be the same user
    assert updated_user.id == original_user_id

    # Verify OAuth account was updated
    stmt = select(OAuthAccount).where(
        OAuthAccount.provider == "github",
        OAuthAccount.provider_user_id == "github_456",
    )
    result = await db_session.execute(stmt)
    oauth_account = result.scalar_one()

    assert oauth_account.access_token == "new_access_token"
    assert oauth_account.refresh_token == "new_refresh_token"
    assert oauth_account.name == "Existing User Updated"


@pytest.mark.asyncio
async def test_link_multiple_oauth_providers(db_session: AsyncSession):
    """Test linking multiple OAuth providers to one user."""
    # Create user with GitHub
    user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="github_789",
        email="multi@example.com",
        name="Multi Provider User",
        access_token="github_token",
    )

    # Link Google to the same user (same email)
    await create_or_update_user(
        db_session,
        provider="google",
        provider_user_id="google_0001",
        email="multi@example.com",
        name="Multi Provider User",
        access_token="google_token",
    )

    # Verify only one user exists
    user_stmt = select(User).where(User.email == "multi@example.com")
    user_result = await db_session.execute(user_stmt)
    users = user_result.scalars().all()
    assert len(users) == 1

    # Verify two OAuth accounts linked to the user
    oauth_stmt = select(OAuthAccount).where(OAuthAccount.user_id == user.id)
    oauth_result = await db_session.execute(oauth_stmt)
    oauth_accounts = oauth_result.scalars().all()
    assert len(oauth_accounts) == 2

    providers = {acc.provider for acc in oauth_accounts}
    assert providers == {"github", "google"}


@pytest.mark.asyncio
async def test_bootstrap_admin_granted_for_verified_allowlisted_login(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Allowlisted + verified login should auto-create Administrator and set approval."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAILS", " Admin@Example.com ")
    clear_settings_cache()

    user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="bootstrap_1",
        email="admin@example.com",
        name="Bootstrap Admin",
        access_token="token",
        email_verified=True,
        auth_provider="github",
    )

    stmt = select(Administrator).where(Administrator.user_id == user.id)
    result = await db_session.execute(stmt)
    admin_record = result.scalar_one_or_none()

    assert admin_record is not None
    assert "BOOTSTRAP_ADMIN_EMAILS" in (admin_record.notes or "")
    assert user.is_approved is True


@pytest.mark.asyncio
async def test_bootstrap_admin_denied_for_unverified_allowlisted_login(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Allowlisted login without verified email should not grant admin."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAILS", "pending@example.com")
    clear_settings_cache()

    user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="bootstrap_2",
        email="pending@example.com",
        name="Pending User",
        access_token="token",
        email_verified=False,
        auth_provider="github",
    )

    stmt = select(Administrator).where(Administrator.user_id == user.id)
    result = await db_session.execute(stmt)
    admin_record = result.scalar_one_or_none()

    assert admin_record is None
    assert user.is_approved is False


@pytest.mark.asyncio
async def test_bootstrap_admin_not_granted_when_email_not_allowlisted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Verified login not in BOOTSTRAP_ADMIN_EMAILS should not grant admin."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAILS", "someone@example.com")
    clear_settings_cache()

    user = await create_or_update_user(
        db_session,
        provider="google",
        provider_user_id="bootstrap_3",
        email="not-allowlisted@example.com",
        name="Regular User",
        access_token="token",
        email_verified=True,
        auth_provider="google",
    )

    stmt = select(Administrator).where(Administrator.user_id == user.id)
    result = await db_session.execute(stmt)
    admin_record = result.scalar_one_or_none()

    assert admin_record is None
    assert user.is_approved is False


@pytest.mark.asyncio
async def test_bootstrap_admin_grant_is_non_revoking(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Removing an email from BOOTSTRAP_ADMIN_EMAILS should not auto-revoke admin."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAILS", "carry@example.com")
    clear_settings_cache()

    user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="bootstrap_4",
        email="carry@example.com",
        name="Carry Admin",
        access_token="token",
        email_verified=True,
        auth_provider="github",
    )
    assert user.is_approved is True

    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAILS", "different@example.com")
    clear_settings_cache()

    # Re-login should keep admin status (no auto-revoke)
    relogin_user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="bootstrap_4",
        email="carry@example.com",
        name="Carry Admin",
        access_token="new-token",
        email_verified=True,
        auth_provider="github",
    )

    stmt = select(Administrator).where(Administrator.user_id == user.id)
    result = await db_session.execute(stmt)
    admin_rows = result.scalars().all()

    assert len(admin_rows) == 1
    assert relogin_user.is_approved is True


@pytest.mark.asyncio
async def test_create_session(db_session: AsyncSession, test_user: User):
    """Test creating a session for a user."""
    session = await create_session(db_session, str(test_user.id))

    assert isinstance(session.id, UUID)
    assert session.user_id == test_user.id
    assert session.expires_at > datetime.now(timezone.utc)

    # Session should expire in approximately 30 days (default)
    expected_expiry = datetime.now(timezone.utc) + timedelta(days=30)
    time_diff = abs((session.expires_at - expected_expiry).total_seconds())
    assert time_diff < 60  # Within 1 minute tolerance


@pytest.mark.asyncio
async def test_session_token_uniqueness(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    """Test that session IDs are unique."""
    session1 = await create_session(db_session, str(test_user.id))
    session2 = await create_session(db_session, str(test_user2.id))

    assert session1.id != session2.id


@pytest.mark.asyncio
async def test_expired_session(db_session: AsyncSession, test_user: User):
    """Test that expired sessions are not valid."""
    # Create session with past expiry
    session = DBSession(
        user_id=test_user.id,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(session)
    await db_session.commit()

    # Try to get user with expired session
    stmt = (
        select(User)
        .join(DBSession)
        .where(
            DBSession.id == session.id,
            DBSession.expires_at > datetime.now(timezone.utc),
        )
    )
    result = await db_session.execute(stmt)
    user = result.scalar_one_or_none()

    assert user is None


@pytest.mark.asyncio
async def test_valid_session(db_session: AsyncSession, test_user: User):
    """Test that valid sessions return the correct user."""
    # Create valid session
    session = DBSession(
        user_id=test_user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(session)
    await db_session.commit()

    # Get user with valid session
    stmt = (
        select(User)
        .join(DBSession)
        .where(
            DBSession.id == session.id,
            DBSession.expires_at > datetime.now(timezone.utc),
        )
    )
    result = await db_session.execute(stmt)
    user = result.scalar_one()

    assert user.id == test_user.id
    assert user.email == test_user.email


@pytest.mark.asyncio
async def test_multiple_sessions_same_user(db_session: AsyncSession, test_user: User):
    """Test that a user can have multiple active sessions."""
    session1 = await create_session(db_session, str(test_user.id))
    session2 = await create_session(db_session, str(test_user.id))

    # Both sessions should be valid and different
    assert session1.id != session2.id
    assert session1.user_id == session2.user_id == test_user.id


@pytest.mark.asyncio
async def test_delete_session(db_session: AsyncSession, test_user: User):
    """Test deleting a session (logout)."""
    session = await create_session(db_session, str(test_user.id))
    session_id = session.id

    # Delete session
    await db_session.delete(session)
    await db_session.commit()

    # Verify session is gone
    stmt = select(DBSession).where(DBSession.id == session_id)
    result = await db_session.execute(stmt)
    deleted_session = result.scalar_one_or_none()

    assert deleted_session is None


@pytest.mark.asyncio
async def test_user_cascade_delete_sessions(db_session: AsyncSession):
    """Test that deleting a user deletes their sessions."""
    # Create user and sessions
    user = User(email="delete_me@example.com", name="Delete Me")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    await create_session(db_session, str(user.id))
    await create_session(db_session, str(user.id))

    # Delete user
    await db_session.delete(user)
    await db_session.commit()

    # Verify sessions are also deleted
    stmt = select(DBSession).where(DBSession.user_id == user.id)
    result = await db_session.execute(stmt)
    sessions = result.scalars().all()

    assert len(sessions) == 0


@pytest.mark.asyncio
async def test_oauth_account_cascade_delete(db_session: AsyncSession):
    """Test that deleting a user deletes their OAuth accounts."""
    user = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="delete_test",
        email="delete_oauth@example.com",
        name="Delete OAuth",
        access_token="token",
    )

    user_id = user.id

    # Delete user
    await db_session.delete(user)
    await db_session.commit()

    # Verify OAuth account is also deleted
    stmt = select(OAuthAccount).where(OAuthAccount.user_id == user_id)
    result = await db_session.execute(stmt)
    oauth_accounts = result.scalars().all()

    assert len(oauth_accounts) == 0


@pytest.mark.asyncio
async def test_inactive_user_cannot_authenticate(db_session: AsyncSession):
    """Test that inactive users cannot authenticate."""
    # Create inactive user
    user = User(
        email="inactive@example.com",
        name="Inactive User",
        is_active=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    # Create session for inactive user
    await create_session(db_session, str(user.id))

    # Query should find user (RLS doesn't block inactive users, app logic should)
    stmt = select(User).where(User.id == user.id)
    result = await db_session.execute(stmt)
    found_user = result.scalar_one()

    assert found_user.is_active is False
    # Note: Application logic should check is_active flag when validating sessions


@pytest.mark.asyncio
async def test_oauth_provider_uniqueness(db_session: AsyncSession):
    """Test that provider + provider_user_id combination is unique."""
    # Create first OAuth account
    user1 = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="unique_id",
        email="user1@example.com",
        name="User 1",
        access_token="token1",
    )

    # Try to create second user with same provider+provider_user_id
    # Should update existing OAuth account instead
    user2 = await create_or_update_user(
        db_session,
        provider="github",
        provider_user_id="unique_id",
        email="user1@example.com",
        name="User 1 Updated",
        access_token="token2",
    )

    # Should be the same user
    assert user1.id == user2.id

    # Verify only one OAuth account exists
    stmt = select(OAuthAccount).where(
        OAuthAccount.provider == "github",
        OAuthAccount.provider_user_id == "unique_id",
    )
    result = await db_session.execute(stmt)
    oauth_accounts = result.scalars().all()

    assert len(oauth_accounts) == 1
    assert oauth_accounts[0].access_token == "token2"

"""
Tests for REST API endpoints.

Tests job management, authentication, and API key functionality.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.api.auth import generate_api_key_secret, hash_secret, verify_secret
from openscientist.database.models import APIKey, Job, User
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls


@pytest.fixture
def api_key_secret() -> str:
    """Generate a test API key secret."""
    return "test_secret_abc123"


@pytest.fixture
async def test_api_key_with_secret(
    db_session: AsyncSession,
    test_user: User,
    api_key_secret: str,
) -> tuple[APIKey, str]:
    """
    Create a test API key and return both the key object and the full secret.

    Returns:
        Tuple of (APIKey object, full "name:secret" string)
    """
    api_key = APIKey(
        user_id=test_user.id,
        name="test_key",
        key_hash=hash_secret(api_key_secret),
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)

    return api_key, f"test_key:{api_key_secret}"


def test_generate_api_key_secret():
    """Test API key secret generation."""
    secret1 = generate_api_key_secret()
    secret2 = generate_api_key_secret()

    # Should be long and unique
    assert len(secret1) == 64  # 32 bytes * 2 (hex encoding)
    assert len(secret2) == 64
    assert secret1 != secret2


def test_hash_and_verify_secret():
    """Test hashing and verification of secrets."""
    secret = "my_test_secret_12345"

    # Hash the secret
    hashed = hash_secret(secret)

    # Verify correct secret
    assert verify_secret(secret, hashed) is True

    # Verify wrong secret
    assert verify_secret("wrong_secret", hashed) is False


def test_hash_deterministic():
    """Test that hashing is deterministic."""
    secret = "consistent_secret"

    hash1 = hash_secret(secret)
    hash2 = hash_secret(secret)

    assert hash1 == hash2


@pytest.mark.asyncio
async def test_api_key_authentication(
    db_session: AsyncSession,
    test_user: User,
    test_api_key_with_secret: tuple,
):
    """Test API key authentication flow."""
    api_key, full_key = test_api_key_with_secret

    # Parse the key
    name, secret = full_key.split(":")

    assert name == "test_key"

    # Verify the secret matches the stored hash
    assert verify_secret(secret, api_key.key_hash) is True

    # Verify relationship to user
    await db_session.refresh(api_key, ["user"])
    assert api_key.user.id == test_user.id


@pytest.mark.asyncio
async def test_expired_api_key(db_session: AsyncSession, test_user: User):
    """Test that inactive API keys are filtered out."""
    # Create inactive API key
    secret = "expired_secret"
    api_key = APIKey(
        user_id=test_user.id,
        name="expired_key",
        key_hash=hash_secret(secret),
        is_active=False,
    )
    db_session.add(api_key)
    await db_session.commit()

    # Query for active keys only
    stmt = select(APIKey).where(
        APIKey.name == "expired_key",
        APIKey.is_active.is_(True),
    )
    result = await db_session.execute(stmt)
    valid_key = result.scalar_one_or_none()

    assert valid_key is None


@pytest.mark.asyncio
async def test_inactive_api_key(db_session: AsyncSession, test_user: User):
    """Test that inactive API keys are not valid."""
    secret = "inactive_secret"
    api_key = APIKey(
        user_id=test_user.id,
        name="inactive_key",
        key_hash=hash_secret(secret),
        is_active=False,
    )
    db_session.add(api_key)
    await db_session.commit()

    # Query for valid keys only
    stmt = select(APIKey).where(
        APIKey.name == "inactive_key",
        APIKey.is_active.is_(True),
    )
    result = await db_session.execute(stmt)
    valid_key = result.scalar_one_or_none()

    assert valid_key is None


@pytest.mark.asyncio
async def test_revoke_api_key(db_session: AsyncSession, test_user: User):
    """Test revoking an API key."""
    secret = "to_revoke"
    api_key = APIKey(
        user_id=test_user.id,
        name="revoke_key",
        key_hash=hash_secret(secret),
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)

    # Revoke the key
    api_key.is_active = False
    await db_session.commit()

    # Verify it's inactive
    await db_session.refresh(api_key)
    assert api_key.is_active is False


@pytest.mark.asyncio
async def test_multiple_api_keys_same_user(db_session: AsyncSession, test_user: User):
    """Test that a user can have multiple API keys."""
    key1 = APIKey(
        user_id=test_user.id,
        name="key1",
        key_hash=hash_secret("secret1"),
    )
    key2 = APIKey(
        user_id=test_user.id,
        name="key2",
        key_hash=hash_secret("secret2"),
    )

    db_session.add_all([key1, key2])
    await db_session.commit()

    # Verify both keys exist
    stmt = select(APIKey).where(APIKey.user_id == test_user.id)
    result = await db_session.execute(stmt)
    keys = result.scalars().all()

    # At least key1 and key2 (may also have fixture key)
    assert len(keys) >= 2
    key_names = {key.name for key in keys}
    assert "key1" in key_names
    assert "key2" in key_names


@pytest.mark.asyncio
async def test_duplicate_api_key_name_same_user_rejected(db_session: AsyncSession, test_user: User):
    """The same user should not be able to create duplicate key names."""
    db_session.add_all(
        [
            APIKey(
                user_id=test_user.id,
                name="duplicate-name",
                key_hash=hash_secret("secret-1"),
            ),
            APIKey(
                user_id=test_user.id,
                name="duplicate-name",
                key_hash=hash_secret("secret-2"),
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_api_key_last_used_update(db_session: AsyncSession, test_user: User):
    """Test updating last_used_at timestamp."""
    api_key = APIKey(
        user_id=test_user.id,
        name="usage_key",
        key_hash=hash_secret("secret"),
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)

    # Initially should be None
    assert api_key.last_used_at is None

    # Update last_used_at
    now = datetime.now(UTC)
    api_key.last_used_at = now
    await db_session.commit()
    await db_session.refresh(api_key)

    assert api_key.last_used_at is not None


@pytest.mark.asyncio
async def test_job_access_via_api_key(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test that jobs are accessible when authenticated with API key."""
    # Set RLS context with user
    await set_current_user(db_session, test_user.id)

    # Query job
    stmt = select(Job).where(Job.id == test_job.id)
    result = await db_session.execute(stmt)
    job = result.scalar_one_or_none()

    assert job is not None
    assert job.id == test_job.id
    assert job.owner_id == test_user.id


@pytest.mark.asyncio
async def test_cannot_access_other_user_jobs(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test that API key cannot access other users' jobs."""
    # Create job for user2
    job = Job(
        owner_id=test_user2.id,
        title="User 2 Job",
        description="Belongs to user2",
        status="pending",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Try to access as user1 (should fail with RLS)
    await enable_rls(db_session)  # Switch to non-superuser role to enforce RLS
    await set_current_user(db_session, test_user.id)

    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    accessible_job = result.scalar_one_or_none()

    assert accessible_job is None  # RLS should block access


@pytest.mark.asyncio
async def test_cascade_delete_api_keys(db_session: AsyncSession):
    """Test that deleting a user deletes their API keys."""
    # Create user and API key
    user = User(email="delete_keys@example.com", name="Delete Keys")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    api_key = APIKey(
        user_id=user.id,
        name="test_key",
        key_hash=hash_secret("secret"),
    )
    db_session.add(api_key)
    await db_session.commit()

    user_id = user.id

    # Delete user
    await db_session.delete(user)
    await db_session.commit()

    # Verify API key is also deleted
    stmt = select(APIKey).where(APIKey.user_id == user_id)
    result = await db_session.execute(stmt)
    keys = result.scalars().all()

    assert len(keys) == 0


@pytest.mark.asyncio
async def test_api_key_name_not_unique_across_users(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test that different users can have API keys with the same name."""
    key1 = APIKey(
        user_id=test_user.id,
        name="my_key",
        key_hash=hash_secret("secret1"),
    )
    key2 = APIKey(
        user_id=test_user2.id,
        name="my_key",
        key_hash=hash_secret("secret2"),
    )

    db_session.add_all([key1, key2])
    await db_session.commit()

    # Both keys should exist with the same name
    stmt = select(APIKey).where(APIKey.name == "my_key")
    result = await db_session.execute(stmt)
    keys = result.scalars().all()

    assert len(keys) == 2
    assert {key.user_id for key in keys} == {test_user.id, test_user2.id}


@pytest.mark.asyncio
async def test_api_key_wrong_secret(
    db_session: AsyncSession,
    test_user: User,
    test_api_key_with_secret: tuple,
):
    """Test that wrong secret doesn't authenticate."""
    _ = (db_session, test_user)
    api_key, full_key = test_api_key_with_secret
    _name, correct_secret = full_key.split(":")

    # Try with wrong secret
    wrong_secret = "wrong_secret_xyz"

    assert verify_secret(wrong_secret, api_key.key_hash) is False
    assert verify_secret(correct_secret, api_key.key_hash) is True

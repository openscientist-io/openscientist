"""Regression tests for test database session fixture behavior."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import hash_secret
from shandy.database.models import APIKey, User


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "error:transaction already deassociated from connection:sqlalchemy.exc.SAWarning"
)
async def test_db_session_fixture_teardown_handles_manual_rollback(
    db_session: AsyncSession,
    test_user: User,
):
    """
    Manual rollback after an IntegrityError should not trigger teardown warnings.

    This specifically guards against SQLAlchemy emitting:
    "transaction already deassociated from connection" on fixture cleanup.
    """
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

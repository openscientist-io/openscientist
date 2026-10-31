"""The notification settings a user changes through their own session."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database import get_session_ctx
from openscientist.database.models import User
from openscientist.database.rls import set_current_user
from openscientist.webapp_components.ui_components import (
    _fetch_notification_settings,
    _set_notifications_enabled,
)


@dataclass(frozen=True)
class _Users:
    alice: UUID
    bob: UUID


@pytest_asyncio.fixture
async def users(test_engine: AsyncEngine) -> AsyncGenerator[_Users, None]:
    """Two committed users."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        alice = User(email=f"alice-{uuid4().hex}@ntfy.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@ntfy.test", name="Bob")
        session.add_all([alice, bob])
        await session.commit()
    yield _Users(alice=alice.id, bob=bob.id)
    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": [alice.id, bob.id]}
        )


async def _ntfy_enabled(test_engine: AsyncEngine, user_id: UUID) -> bool:
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT ntfy_enabled FROM users WHERE id = :id"), {"id": user_id}
        )
        return bool(result.scalar_one())


@pytest.mark.asyncio
async def test_turning_notifications_off_and_on_is_saved(
    users: _Users, test_engine: AsyncEngine
) -> None:
    assert await _ntfy_enabled(test_engine, users.alice)

    await _set_notifications_enabled(str(users.alice), False)
    assert not await _ntfy_enabled(test_engine, users.alice)
    assert await _fetch_notification_settings(str(users.alice)) == (False, None)

    await _set_notifications_enabled(str(users.alice), True)
    assert await _ntfy_enabled(test_engine, users.alice)


@pytest.mark.asyncio
async def test_a_user_session_cannot_change_another_users_settings(
    users: _Users, test_engine: AsyncEngine
) -> None:
    async with get_session_ctx() as session:
        await set_current_user(session, users.alice)
        await session.execute(update(User).where(User.id == users.bob).values(ntfy_enabled=False))
        await session.commit()
    assert await _ntfy_enabled(test_engine, users.bob)


@pytest.mark.asyncio
async def test_a_user_session_cannot_approve_itself(users: _Users) -> None:
    """The update policy opens the user's own row, so only the column grant
    keeps the other columns fixed."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with get_session_ctx() as session:
            await set_current_user(session, users.alice)
            await session.execute(
                update(User).where(User.id == users.alice).values(is_approved=True)
            )


@pytest.mark.asyncio
async def test_a_user_session_cannot_make_itself_a_priority_user(users: _Users) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with get_session_ctx() as session:
            await set_current_user(session, users.alice)
            await session.execute(
                update(User).where(User.id == users.alice).values(is_priority=True)
            )

"""The feedback users send from the jobs page and the admins' read state on it."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import User, UserFeedback
from openscientist.database.session import thread_safe_user_session
from openscientist.webapp_components.components.feedback import (
    FeedbackLimitReachedError,
    submit_feedback,
)
from openscientist.webapp_components.pages.feedback_panel import (
    list_feedback,
    set_feedback_read,
)

_MIGRATION = (
    Path(__file__).parent.parent
    / "src/openscientist/database/migrations/versions"
    / "2026-11-18_1200_user_feedback.py"
)


@dataclass(frozen=True)
class _Users:
    alice: UUID
    bob: UUID
    admin: UUID


@pytest_asyncio.fixture
async def users(test_engine: AsyncEngine) -> AsyncGenerator[_Users, None]:
    """Two committed users and an admin, whose feedback the test deletes when done."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        alice = User(email=f"alice-{uuid4().hex}@feedback.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@feedback.test", name="Bob")
        admin = User(email=f"admin-{uuid4().hex}@feedback.test", name="Admin")
        session.add_all([alice, bob, admin])
        await session.commit()
    ids = [alice.id, bob.id, admin.id]
    yield _Users(alice=alice.id, bob=bob.id, admin=admin.id)
    async with AsyncSession(test_engine) as session:
        await session.execute(delete(UserFeedback).where(UserFeedback.user_id.in_(ids)))
        await session.execute(delete(User).where(User.id.in_(ids)))
        await session.commit()


async def _feedback_of(test_engine: AsyncEngine, user_id: UUID) -> list[UserFeedback]:
    async with AsyncSession(test_engine) as session:
        result = await session.execute(select(UserFeedback).where(UserFeedback.user_id == user_id))
        return list(result.scalars())


async def _add_sent(test_engine: AsyncEngine, user_id: UUID, count: int, age_hours: int) -> None:
    """``count`` messages from ``user_id``, sent ``age_hours`` ago."""
    created_at = datetime.now(UTC) - timedelta(hours=age_hours)
    async with AsyncSession(test_engine) as session:
        session.add_all(
            UserFeedback(user_id=user_id, message=f"Earlier {n}", created_at=created_at)
            for n in range(count)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_sender_is_refused_after_ten_messages_in_a_day(
    users: _Users, test_engine: AsyncEngine
) -> None:
    for n in range(10):
        await submit_feedback(users.alice, f"Message {n}")
    with pytest.raises(FeedbackLimitReachedError):
        await submit_feedback(users.alice, "One too many")

    assert len(await _feedback_of(test_engine, users.alice)) == 10
    await submit_feedback(users.bob, "Bob has his own allowance")


@pytest.mark.asyncio
async def test_messages_older_than_a_day_leave_the_allowance(
    users: _Users, test_engine: AsyncEngine
) -> None:
    await _add_sent(test_engine, users.alice, 10, age_hours=25)

    await submit_feedback(users.alice, "A new day")

    assert len(await _feedback_of(test_engine, users.alice)) == 11


@pytest.mark.asyncio
async def test_concurrent_sends_stop_at_the_daily_limit(
    users: _Users, test_engine: AsyncEngine
) -> None:
    await _add_sent(test_engine, users.alice, 8, age_hours=1)

    outcomes = await asyncio.gather(
        *(submit_feedback(users.alice, f"Burst {n}") for n in range(5)),
        return_exceptions=True,
    )

    refused = [o for o in outcomes if isinstance(o, FeedbackLimitReachedError)]
    assert (len(refused), outcomes.count(None)) == (3, 2)
    assert len(await _feedback_of(test_engine, users.alice)) == 10


@pytest.mark.asyncio
async def test_sent_feedback_is_stored_unread_under_the_senders_name(
    users: _Users, test_engine: AsyncEngine
) -> None:
    await submit_feedback(users.alice, "The results page was confusing")

    [stored] = await _feedback_of(test_engine, users.alice)
    assert stored.message == "The results page was confusing"
    assert stored.read_at is None
    assert stored.read_by is None


@pytest.mark.asyncio
async def test_a_user_cannot_send_feedback_in_someone_elses_name(
    users: _Users, test_engine: AsyncEngine
) -> None:
    with pytest.raises(DBAPIError):
        async with thread_safe_user_session(users.alice) as session:
            await session.execute(
                insert(UserFeedback).values(user_id=users.bob, message="Not from Bob")
            )
            await session.commit()
    assert await _feedback_of(test_engine, users.bob) == []


@pytest.mark.asyncio
async def test_a_user_cannot_send_feedback_already_marked_read(
    users: _Users, test_engine: AsyncEngine
) -> None:
    with pytest.raises(DBAPIError):
        async with thread_safe_user_session(users.alice) as session:
            await session.execute(
                insert(UserFeedback).values(
                    user_id=users.alice, message="Hidden", read_at=datetime.now(UTC)
                )
            )
            await session.commit()
    assert await _feedback_of(test_engine, users.alice) == []


@pytest.mark.asyncio
async def test_a_user_session_can_neither_read_nor_mark_any_feedback(users: _Users) -> None:
    await submit_feedback(users.alice, "Mine")
    with pytest.raises(DBAPIError):
        async with thread_safe_user_session(users.alice) as session:
            await session.execute(select(UserFeedback.message))
    with pytest.raises(DBAPIError):
        async with thread_safe_user_session(users.alice) as session:
            await session.execute(update(UserFeedback).values(read_at=datetime.now(UTC)))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["", "x" * 5001])
async def test_the_database_refuses_an_empty_or_overlong_message(
    users: _Users, message: str
) -> None:
    with pytest.raises(DBAPIError):
        await submit_feedback(users.alice, message)


async def _add(
    session: AsyncSession, user_id: UUID | None, message: str, age_hours: int, *, read: bool
) -> UserFeedback:
    created_at = datetime.now(UTC) - timedelta(hours=age_hours)
    feedback = UserFeedback(
        user_id=user_id,
        message=message,
        created_at=created_at,
        read_at=created_at if read else None,
    )
    session.add(feedback)
    await session.flush()
    return feedback


@pytest.mark.asyncio
async def test_the_admin_list_puts_unread_feedback_first_and_newest_first(
    db_session: AsyncSession, test_user: User
) -> None:
    await db_session.execute(delete(UserFeedback))
    await _add(db_session, test_user.id, "old unread", 3, read=False)
    await _add(db_session, test_user.id, "newest but read", 1, read=True)
    await _add(db_session, None, "new unread from a deleted user", 2, read=False)

    entries = await list_feedback(db_session)

    assert [(entry.feedback.message, entry.sender) for entry in entries] == [
        ("new unread from a deleted user", None),
        ("old unread", test_user),
        ("newest but read", test_user),
    ]


@pytest.mark.asyncio
async def test_an_admin_marks_feedback_read_and_unread(
    db_session: AsyncSession, test_user: User, test_admin_user: User
) -> None:
    feedback = await _add(db_session, test_user.id, "Please add a dark mode", 1, read=False)

    assert await set_feedback_read(db_session, feedback.id, read=True, admin_id=test_admin_user.id)
    await db_session.refresh(feedback)
    assert feedback.read_at is not None
    assert feedback.read_by == test_admin_user.id

    assert await set_feedback_read(db_session, feedback.id, read=False, admin_id=test_admin_user.id)
    await db_session.refresh(feedback)
    assert feedback.read_at is None
    assert feedback.read_by is None


@pytest.mark.asyncio
async def test_marking_missing_feedback_reports_failure(
    db_session: AsyncSession, test_admin_user: User
) -> None:
    assert not await set_feedback_read(db_session, uuid4(), read=True, admin_id=test_admin_user.id)


@pytest.mark.asyncio
async def test_deleting_a_user_keeps_their_feedback(
    users: _Users, test_engine: AsyncEngine
) -> None:
    await submit_feedback(users.bob, "Kept after Bob leaves")
    async with AsyncSession(test_engine) as session:
        await session.execute(delete(User).where(User.id == users.bob))
        await session.commit()
        result = await session.execute(
            select(UserFeedback).where(UserFeedback.message == "Kept after Bob leaves")
        )
        kept = result.scalar_one()
        assert kept.user_id is None
        await session.delete(kept)
        await session.commit()


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("user_feedback", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_table(connection: Connection) -> bool:
    return (
        connection.execute(text("SELECT to_regclass('public.user_feedback')")).scalar() is not None
    )


@pytest.mark.asyncio
async def test_the_migration_downgrades_and_upgrades_cleanly(test_engine: AsyncEngine) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert not _has_table(connection)
                module.upgrade()
            assert _has_table(connection)
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)

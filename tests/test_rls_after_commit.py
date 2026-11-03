"""A user session stays under RLS for its whole life, commits included, and
leaves nothing of its role or user on the pooled connections it used."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.engine import get_engine
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session_ctx
from openscientist.job.repository import _job_session

_WHO = text("SELECT current_user, current_setting('app.current_user_id', true)")


@dataclass(frozen=True)
class _TwoOwners:
    alice: UUID
    alice_job: UUID
    bob_job: UUID


@pytest_asyncio.fixture
async def two_owners(test_engine: AsyncEngine) -> AsyncGenerator[_TwoOwners, None]:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        alice = User(email=f"alice-{uuid4().hex}@rls.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@rls.test", name="Bob")
        session.add_all([alice, bob])
        await session.flush()
        alice_job = Job(owner_id=alice.id, research_question="Alice's")
        bob_job = Job(owner_id=bob.id, research_question="Bob's")
        session.add_all([alice_job, bob_job])
        await session.commit()
    yield _TwoOwners(alice=alice.id, alice_job=alice_job.id, bob_job=bob_job.id)
    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM jobs WHERE id IN (:a, :b)"), {"a": alice_job.id, "b": bob_job.id}
        )
        await conn.execute(
            text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": alice.id, "b": bob.id}
        )


async def _warm_pool(idle: int) -> None:
    """Leave ``idle`` connections in the shared pool, so a session that gives its
    connection back at commit gets a different one next."""
    engine = get_engine()
    connections = [await engine.connect() for _ in range(idle)]
    for conn in connections:
        await conn.close()


async def _visible_jobs(session: AsyncSession, owners: _TwoOwners) -> set[UUID]:
    rows = await session.execute(
        text("SELECT id FROM jobs WHERE id IN (:a, :b)"),
        {"a": owners.alice_job, "b": owners.bob_job},
    )
    return {row.id for row in rows}


@pytest.mark.asyncio
async def test_get_session_stays_under_rls_after_a_commit(two_owners: _TwoOwners) -> None:
    await _warm_pool(2)
    async with get_session_ctx() as session:
        await set_current_user(session, two_owners.alice)
        assert await _visible_jobs(session, two_owners) == {two_owners.alice_job}
        await session.commit()
        assert await _visible_jobs(session, two_owners) == {two_owners.alice_job}
        who = tuple((await session.execute(_WHO)).one())
    assert who == ("openscientist_app", str(two_owners.alice))


@pytest.mark.asyncio
async def test_a_thread_safe_user_session_stays_under_rls_after_a_commit(
    two_owners: _TwoOwners,
) -> None:
    async with _job_session(two_owners.alice) as session:
        await session.commit()
        assert await _visible_jobs(session, two_owners) == {two_owners.alice_job}
        who = tuple((await session.execute(_WHO)).one())
    assert who == ("openscientist_app", str(two_owners.alice))


@pytest.mark.asyncio
async def test_a_user_session_leaves_no_role_or_user_on_pooled_connections(
    two_owners: _TwoOwners,
) -> None:
    await _warm_pool(2)
    async with get_session_ctx() as session:
        await set_current_user(session, two_owners.alice)
        await _visible_jobs(session, two_owners)
        await session.commit()
        await _visible_jobs(session, two_owners)
    engine = get_engine()
    held = [await engine.connect() for _ in range(2)]
    try:
        seen = {tuple((await conn.execute(_WHO)).one()) for conn in held}
    finally:
        for conn in held:
            await conn.close()
    assert {who for who, _ in seen} == {engine.url.username}
    assert {user or None for _, user in seen} == {None}

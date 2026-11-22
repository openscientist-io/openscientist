"""Postgres itself decides who may grant and remove admin rights.

A user session inserts an ``administrators`` row only while its user is an
admin and names itself as the granter, and deletes one only while its user is
an admin and the row is someone else's. The checks hold for raw SQL on the
app role, so they do not depend on the page calling the right helper.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.admin.administrators import grant_admin, revoke_admin
from openscientist.database.models import Administrator, User
from openscientist.database.session import thread_safe_user_session
from tests.db_outage import Forwarder
from tests.helpers import advisory_lock_waiter


@dataclass(frozen=True)
class _Cast:
    alice: UUID
    bob: UUID
    carol: UUID


@pytest_asyncio.fixture
async def cast(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[_Cast]:
    """Admins Alice and Bob, and Carol, who is not an admin."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        alice = User(email=f"alice-{uuid4().hex}@admins.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@admins.test", name="Bob")
        carol = User(email=f"carol-{uuid4().hex}@admins.test", name="Carol")
        db.add_all([alice, bob, carol])
        await db.flush()
        db.add_all([Administrator(user_id=alice.id), Administrator(user_id=bob.id)])
        await db.commit()
    yield _Cast(alice.id, bob.id, carol.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id.in_([alice.id, bob.id, carol.id])))
        await db.commit()


async def _admins(test_engine: AsyncEngine, cast: _Cast) -> dict[UUID, UUID | None]:
    """Each admin among the cast, with the user who granted the rights."""
    async with AsyncSession(test_engine) as db:
        rows = await db.execute(
            select(Administrator.user_id, Administrator.granted_by).where(
                Administrator.user_id.in_([cast.alice, cast.bob, cast.carol])
            )
        )
        return dict(rows.tuples().all())


async def _insert(actor: UUID, user_id: UUID, granted_by: UUID) -> None:
    async with thread_safe_user_session(actor) as session:
        await session.execute(
            text("INSERT INTO administrators (user_id, granted_by) VALUES (:user_id, :granted_by)"),
            {"user_id": user_id, "granted_by": granted_by},
        )
        await session.commit()


async def _delete(actor: UUID, user_id: UUID) -> None:
    async with thread_safe_user_session(actor) as session:
        await session.execute(
            text("DELETE FROM administrators WHERE user_id = :user_id"), {"user_id": user_id}
        )
        await session.commit()


@pytest.mark.asyncio
async def test_an_admin_grants_admin_rights_recorded_as_granted_by_them(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    assert await grant_admin(cast.alice, cast.carol) == (True, "User is now an admin")
    assert (await _admins(test_engine, cast))[cast.carol] == cast.alice


@pytest.mark.asyncio
async def test_postgres_refuses_a_grant_from_a_user_who_is_not_an_admin(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    with pytest.raises(DBAPIError, match="only admins can change admin rights"):
        await _insert(cast.carol, cast.carol, cast.carol)
    assert await grant_admin(cast.carol, cast.carol) == (
        False,
        "Only admins can change admin rights",
    )
    assert cast.carol not in await _admins(test_engine, cast)


@pytest.mark.asyncio
async def test_postgres_refuses_a_grant_recorded_as_another_admins(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        await _insert(cast.alice, cast.carol, cast.bob)
    assert cast.carol not in await _admins(test_engine, cast)


@pytest.mark.asyncio
async def test_granting_an_existing_admin_or_a_missing_user_changes_nothing(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    before = await _admins(test_engine, cast)
    assert await grant_admin(cast.alice, cast.bob) == (False, "User is already an admin")
    assert await grant_admin(cast.alice, uuid4()) == (False, "User not found")
    assert await _admins(test_engine, cast) == before


@pytest.mark.asyncio
async def test_an_admin_removes_another_admins_rights(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    assert await revoke_admin(cast.alice, cast.bob) == (True, "User is no longer an admin")
    assert set(await _admins(test_engine, cast)) == {cast.alice}
    assert await revoke_admin(cast.alice, cast.bob) == (False, "User is not an admin")


@pytest.mark.asyncio
async def test_postgres_refuses_an_admin_removing_their_own_rights(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    with pytest.raises(DBAPIError, match="cannot remove their own admin rights"):
        await _delete(cast.alice, cast.alice)
    assert await revoke_admin(cast.alice, cast.alice) == (
        False,
        "You cannot remove your own admin rights",
    )
    assert cast.alice in await _admins(test_engine, cast)


@pytest.mark.asyncio
async def test_postgres_refuses_a_removal_from_a_user_who_is_not_an_admin(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    await _delete(cast.carol, cast.alice)
    assert await revoke_admin(cast.carol, cast.alice) == (
        False,
        "Only admins can change admin rights",
    )
    assert set(await _admins(test_engine, cast)) == {cast.alice, cast.bob}


@pytest.mark.asyncio
async def test_two_admins_removing_each_other_at_once_leave_one_admin(
    cast: _Cast, test_engine: AsyncEngine
) -> None:
    async with thread_safe_user_session(cast.alice) as alice_session:
        await alice_session.execute(
            text("DELETE FROM administrators WHERE user_id = :user_id"), {"user_id": cast.bob}
        )
        bob_removes_alice = asyncio.create_task(_delete(cast.bob, cast.alice))
        await advisory_lock_waiter(test_engine, deadline_seconds=10)
        await alice_session.commit()
    with pytest.raises(DBAPIError, match="only admins can change admin rights"):
        await asyncio.wait_for(bob_removes_alice, timeout=10)
    assert set(await _admins(test_engine, cast)) == {cast.alice}


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", [True, False], ids=["grant", "revoke"])
async def test_an_admin_removed_while_changing_admin_rights_changes_nothing(
    cast: _Cast, test_engine: AsyncEngine, grant: bool
) -> None:
    async with thread_safe_user_session(cast.bob) as bob_session:
        await bob_session.execute(
            text("DELETE FROM administrators WHERE user_id = :user_id"), {"user_id": cast.alice}
        )
        change = (
            grant_admin(cast.alice, cast.carol) if grant else revoke_admin(cast.alice, cast.bob)
        )
        alice_changes = asyncio.create_task(change)
        await advisory_lock_waiter(test_engine, deadline_seconds=10)
        await bob_session.commit()
    assert await asyncio.wait_for(alice_changes, timeout=10) == (
        False,
        "Only admins can change admin rights",
    )
    assert set(await _admins(test_engine, cast)) == {cast.bob}

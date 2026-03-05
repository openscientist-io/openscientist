"""
Tests for RLS enforcement on production query patterns.

Verifies that the query patterns used by webapp pages correctly
enforce RLS when using get_session() + set_current_user().
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls


@pytest.mark.asyncio
async def test_create_job_then_get_as_owner(db_session: AsyncSession):
    """Owner can read their own job via RLS-filtered query."""
    user = User(email="owner_prod@example.com", name="Owner")
    db_session.add(user)
    await db_session.commit()

    job = Job(owner_id=user.id, title="Owner's research")
    db_session.add(job)
    await db_session.commit()

    # Switch to app role (subject to RLS)
    await enable_rls(db_session)
    await set_current_user(db_session, user.id)

    # Owner should find their job by ID
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    found = result.scalar_one_or_none()
    assert found is not None
    assert found.title == "Owner's research"


@pytest.mark.asyncio
async def test_create_job_then_get_as_other_user(db_session: AsyncSession):
    """Non-owner cannot read another user's job via RLS-filtered query."""
    alice = User(email="alice_prod@example.com", name="Alice")
    bob = User(email="bob_prod@example.com", name="Bob")
    db_session.add_all([alice, bob])
    await db_session.commit()

    alice_job = Job(owner_id=alice.id, title="Alice's private research")
    db_session.add(alice_job)
    await db_session.commit()

    # Switch to app role and set Bob as current user
    await enable_rls(db_session)
    await set_current_user(db_session, bob.id)

    # Bob should NOT find Alice's job
    result = await db_session.execute(select(Job).where(Job.id == alice_job.id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_list_jobs_with_rls_filters_by_owner(db_session: AsyncSession):
    """Listing jobs under RLS returns only the current user's jobs."""
    alice = User(email="alice_list@example.com", name="Alice")
    bob = User(email="bob_list@example.com", name="Bob")
    db_session.add_all([alice, bob])
    await db_session.commit()

    alice_job = Job(owner_id=alice.id, title="Alice's job")
    bob_job = Job(owner_id=bob.id, title="Bob's job")
    db_session.add_all([alice_job, bob_job])
    await db_session.commit()

    await enable_rls(db_session)

    # Alice should only see her own job
    await set_current_user(db_session, alice.id)
    result = await db_session.execute(select(Job).order_by(Job.created_at.desc()))
    alice_jobs = result.scalars().all()
    assert len(alice_jobs) == 1
    assert alice_jobs[0].title == "Alice's job"

    # Bob should only see his own job
    await set_current_user(db_session, bob.id)
    result = await db_session.execute(select(Job).order_by(Job.created_at.desc()))
    bob_jobs = result.scalars().all()
    assert len(bob_jobs) == 1
    assert bob_jobs[0].title == "Bob's job"


@pytest.mark.asyncio
async def test_get_session_query_filters_by_owner(test_engine):
    """Reproduces exact jobs_list.py query path with SET ROLE + set_current_user.

    This test mirrors how get_session() works: connect as the main user,
    SET ROLE openscientist_app, then set_current_user. Verifies that RLS correctly
    isolates each user's jobs.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openscientist.database.session import _set_app_role

    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    # Create test data as admin
    async with session_factory() as admin_session:
        await admin_session.execute(text("SET ROLE openscientist_admin"))

        alice = User(email="alice_gs_prod@example.com", name="Alice")
        bob = User(email="bob_gs_prod@example.com", name="Bob")
        admin_session.add_all([alice, bob])
        await admin_session.commit()

        alice_job = Job(owner_id=alice.id, title="Alice production job")
        bob_job = Job(owner_id=bob.id, title="Bob production job")
        admin_session.add_all([alice_job, bob_job])
        await admin_session.commit()

        alice_id = alice.id
        bob_id = bob.id

    # Simulate get_session() path: SET ROLE openscientist_app + set_current_user
    async with session_factory() as user_session:
        await _set_app_role(user_session)
        await set_current_user(user_session, alice_id)

        result = await user_session.execute(select(Job).order_by(Job.created_at.desc()))
        visible = result.scalars().all()

        assert len(visible) == 1
        assert visible[0].title == "Alice production job"

    # Same path for Bob
    async with session_factory() as user_session:
        await _set_app_role(user_session)
        await set_current_user(user_session, bob_id)

        result = await user_session.execute(select(Job).order_by(Job.created_at.desc()))
        visible = result.scalars().all()

        assert len(visible) == 1
        assert visible[0].title == "Bob production job"


@pytest.mark.asyncio
async def test_list_jobs_without_rls_returns_all(db_session: AsyncSession):
    """Without RLS (admin session), all jobs are visible regardless of owner.

    Documents intentional behavior: internal operations (scheduler,
    admin endpoints) use admin sessions that bypass RLS.
    """
    alice = User(email="alice_admin@example.com", name="Alice")
    bob = User(email="bob_admin@example.com", name="Bob")
    db_session.add_all([alice, bob])
    await db_session.commit()

    alice_job = Job(owner_id=alice.id, title="Alice admin-visible")
    bob_job = Job(owner_id=bob.id, title="Bob admin-visible")
    db_session.add_all([alice_job, bob_job])
    await db_session.commit()

    # Admin session (no enable_rls) sees all jobs
    result = await db_session.execute(select(Job))
    all_jobs = result.scalars().all()
    titles = {j.title for j in all_jobs}
    assert len(all_jobs) >= 2
    assert "Alice admin-visible" in titles
    assert "Bob admin-visible" in titles

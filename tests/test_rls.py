"""
Tests for Row-Level Security (RLS) functionality.

Verifies that:
1. RLS policies are properly enforced
2. Users can only access their own data
3. Job sharing permissions work correctly
4. Admin bypass functionality works
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database import (
    bypass_rls,
    list_rls_policies,
    session_with_user,
    set_current_user,
    verify_rls_enabled,
)
from shandy.database.models import Job, JobShare, User


@pytest.mark.asyncio
async def test_rls_enabled_on_tables(db_session: AsyncSession):
    """Verify that RLS is enabled on all expected tables."""
    # Check critical tables
    assert await verify_rls_enabled(db_session, "users")
    assert await verify_rls_enabled(db_session, "jobs")
    assert await verify_rls_enabled(db_session, "job_shares")
    assert await verify_rls_enabled(db_session, "api_keys")
    assert await verify_rls_enabled(db_session, "hypotheses")
    assert await verify_rls_enabled(db_session, "findings")


@pytest.mark.asyncio
async def test_user_isolation(db_session: AsyncSession):
    """Test that users can only see their own jobs without sharing."""
    # Create two users and jobs (bypass RLS for setup)
    async with bypass_rls(db_session):
        user1 = User(
            email="user1@example.com",
            name="User One",
        )
        user2 = User(
            email="user2@example.com",
            name="User Two",
        )
        db_session.add_all([user1, user2])
        await db_session.commit()

        job1 = Job(
            owner_id=user1.id,
            title="User 1 Job",
            description="Job belonging to user 1",
        )
        job2 = Job(
            owner_id=user2.id,
            title="User 2 Job",
            description="Job belonging to user 2",
        )
        db_session.add_all([job1, job2])
        await db_session.commit()

    # User 1 should only see their job
    await set_current_user(db_session, user1.id)
    result = await db_session.execute(select(Job))
    user1_jobs = result.scalars().all()
    assert len(user1_jobs) == 1
    assert user1_jobs[0].title == "User 1 Job"

    # User 2 should only see their job
    await set_current_user(db_session, user2.id)
    result = await db_session.execute(select(Job))
    user2_jobs = result.scalars().all()
    assert len(user2_jobs) == 1
    assert user2_jobs[0].title == "User 2 Job"


@pytest.mark.asyncio
async def test_job_sharing_view_permission(db_session: AsyncSession):
    """Test that view permission grants read-only access."""
    # Create two users and a job (bypass RLS for setup)
    async with bypass_rls(db_session):
        owner = User(email="owner@example.com", name="Owner")
        viewer = User(email="viewer@example.com", name="Viewer")
        db_session.add_all([owner, viewer])
        await db_session.commit()

        job = Job(
            owner_id=owner.id,
            title="Shared Job",
            description="Job shared with viewer",
        )
        db_session.add(job)
        await db_session.commit()

        # Share the job with view permission
        share = JobShare(
            job_id=job.id,
            shared_with_user_id=viewer.id,
            permission_level="view",
        )
        db_session.add(share)
        await db_session.commit()

    # Viewer should be able to read the job
    await set_current_user(db_session, viewer.id)
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    viewed_job = result.scalar_one_or_none()
    assert viewed_job is not None
    assert viewed_job.title == "Shared Job"


@pytest.mark.asyncio
async def test_job_sharing_edit_permission(db_session: AsyncSession):
    """Test that edit permission grants full access."""
    # Create two users and a job (bypass RLS for setup)
    async with bypass_rls(db_session):
        owner = User(email="owner2@example.com", name="Owner 2")
        editor = User(email="editor@example.com", name="Editor")
        db_session.add_all([owner, editor])
        await db_session.commit()

        job = Job(
            owner_id=owner.id,
            title="Editable Job",
            description="Job shared with editor",
        )
        db_session.add(job)
        await db_session.commit()

        # Share the job with edit permission
        share = JobShare(
            job_id=job.id,
            shared_with_user_id=editor.id,
            permission_level="edit",
        )
        db_session.add(share)
        await db_session.commit()

    # Editor should be able to read and update the job
    await set_current_user(db_session, editor.id)
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    edited_job = result.scalar_one()

    # Update the job
    edited_job.title = "Updated by Editor"
    db_session.add(edited_job)
    await db_session.commit()

    # Verify the update persisted
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    job_check = result.scalar_one()
    assert job_check.title == "Updated by Editor"


@pytest.mark.asyncio
async def test_bypass_rls(db_session: AsyncSession):
    """Test that bypass_rls allows admin access to all rows."""
    # Create data for multiple users (all with bypass_rls)
    async with bypass_rls(db_session):
        user1 = User(email="user_a@example.com", name="User A")
        user2 = User(email="user_b@example.com", name="User B")
        db_session.add_all([user1, user2])
        await db_session.commit()

        job1 = Job(owner_id=user1.id, title="Job A")
        job2 = Job(owner_id=user2.id, title="Job B")
        db_session.add_all([job1, job2])
        await db_session.commit()

    # Without bypass, user1 should only see their job
    await set_current_user(db_session, user1.id)
    result = await db_session.execute(select(Job))
    jobs = result.scalars().all()
    assert len(jobs) == 1

    #  With bypass, should see all jobs
    async with bypass_rls(db_session):
        result = await db_session.execute(select(Job))
        all_jobs = result.scalars().all()
        assert len(all_jobs) == 2


@pytest.mark.asyncio
async def test_session_with_user_context(db_session: AsyncSession):
    """Test the session_with_user context manager."""
    # Create test data
    async with bypass_rls(db_session):
        user = User(email="context@example.com", name="Context User")
        db_session.add(user)
        await db_session.commit()

        job = Job(owner_id=user.id, title="Context Job")
        db_session.add(job)
        await db_session.commit()

    # Use session_with_user
    async with session_with_user(db_session, user.id):
        result = await db_session.execute(select(Job))
        jobs = result.scalars().all()
        assert len(jobs) == 1
        assert jobs[0].title == "Context Job"


@pytest.mark.asyncio
async def test_orphaned_jobs_visible(db_session: AsyncSession):
    """Test that orphaned jobs (owner_id=NULL) are visible to all users."""
    # Create orphaned job
    async with bypass_rls(db_session):
        orphaned = Job(owner_id=None, title="Orphaned Job")
        db_session.add(orphaned)
        await db_session.commit()

        user = User(email="viewer@example.com", name="Viewer")
        db_session.add(user)
        await db_session.commit()

    # User should be able to see orphaned job
    await set_current_user(db_session, user.id)
    result = await db_session.execute(select(Job))
    jobs = result.scalars().all()
    assert len(jobs) == 1
    assert jobs[0].title == "Orphaned Job"


@pytest.mark.asyncio
async def test_list_rls_policies(db_session: AsyncSession):
    """Test listing RLS policies."""
    policies = await list_rls_policies(db_session, "jobs")
    assert len(policies) > 0
    # Should have policies for jobs table
    policy_names = [p["name"] for p in policies]
    assert any("jobs" in name for name in policy_names)

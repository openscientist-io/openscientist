"""
Tests for job sharing functionality.

Tests sharing permissions, access control, and RLS enforcement.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Job, JobShare, User
from shandy.database.rls import bypass_rls, set_current_user


@pytest.mark.asyncio
async def test_share_job_with_view_permission(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test sharing a job with view permission."""
    # Create job owned by test_user
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Shared Job",
            description="Job to share",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Share with test_user2 (view permission)
    async with bypass_rls(db_session):
        share = JobShare(
            job_id=job.id,
            shared_with_user_id=test_user2.id,
            permission_level="view",
        )
        db_session.add(share)
        await db_session.commit()
        await db_session.refresh(share)

    # Verify share was created
    assert isinstance(share.id, UUID)
    assert share.job_id == job.id
    assert share.shared_with_user_id == test_user2.id
    assert share.permission_level == "view"

    # Verify test_user2 can now see the job (via RLS)
    await set_current_user(db_session, test_user2.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    accessible_job = result.scalar_one_or_none()

    assert accessible_job is not None
    assert accessible_job.id == job.id


@pytest.mark.asyncio
async def test_share_job_with_edit_permission(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test sharing a job with edit permission."""
    # Create job owned by test_user
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Editable Shared Job",
            description="Can be edited by others",
            status="running",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Share with edit permission
    async with bypass_rls(db_session):
        share = JobShare(
            job_id=job.id,
            shared_with_user_id=test_user2.id,
            permission_level="edit",
        )
        db_session.add(share)
        await db_session.commit()

    # Verify test_user2 can access and potentially edit
    await set_current_user(db_session, test_user2.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    accessible_job = result.scalar_one_or_none()

    assert accessible_job is not None
    assert accessible_job.owner_id == test_user.id  # Still owned by original user

    # Verify share permission
    async with bypass_rls(db_session):
        share_stmt = select(JobShare).where(
            JobShare.job_id == job.id,
            JobShare.shared_with_user_id == test_user2.id,
        )
        share_result = await db_session.execute(share_stmt)
        job_share = share_result.scalar_one()

    assert job_share.permission_level == "edit"


@pytest.mark.asyncio
async def test_unshared_job_not_accessible(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test that unshared jobs are not accessible to other users."""
    # Create job owned by test_user
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Private Job",
            description="Not shared",
            status="pending",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Try to access as test_user2 (should fail)
    await set_current_user(db_session, test_user2.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    accessible_job = result.scalar_one_or_none()

    assert accessible_job is None  # RLS should block access


@pytest.mark.asyncio
async def test_revoke_job_share(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test revoking a job share."""
    # Create and share job
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Temporarily Shared",
            description="Will be unshared",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    async with bypass_rls(db_session):
        share = JobShare(
            job_id=job.id,
            shared_with_user_id=test_user2.id,
            permission_level="view",
        )
        db_session.add(share)
        await db_session.commit()

    # Verify test_user2 can access
    await set_current_user(db_session, test_user2.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is not None

    # Revoke share
    async with bypass_rls(db_session):
        await db_session.delete(share)
        await db_session.commit()

    # Verify test_user2 can no longer access
    await set_current_user(db_session, test_user2.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_share_job_with_multiple_users(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test sharing a job with multiple users."""
    # Create third user
    async with bypass_rls(db_session):
        user3 = User(email="user3@example.com", name="User 3")
        db_session.add(user3)
        await db_session.commit()
        await db_session.refresh(user3)

    # Create job
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Multi-Shared Job",
            description="Shared with multiple users",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Share with multiple users
    async with bypass_rls(db_session):
        share1 = JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="view")
        share2 = JobShare(job_id=job.id, shared_with_user_id=user3.id, permission_level="edit")

        db_session.add_all([share1, share2])
        await db_session.commit()

    # Verify both users can access
    await set_current_user(db_session, test_user2.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is not None

    await set_current_user(db_session, user3.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is not None

    # Verify shares with different permissions
    async with bypass_rls(db_session):
        shares_stmt = select(JobShare).where(JobShare.job_id == job.id)
        shares_result = await db_session.execute(shares_stmt)
        shares = shares_result.scalars().all()

        assert len(shares) == 2
        permissions = {(s.shared_with_user_id, s.permission_level) for s in shares}
        assert permissions == {
            (test_user2.id, "view"),
            (user3.id, "edit"),
        }


@pytest.mark.asyncio
async def test_update_share_permission(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test updating share permission from view to edit."""
    # Create and share job
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Permission Update Test",
            description="Test permission change",
            status="running",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    async with bypass_rls(db_session):
        share = JobShare(
            job_id=job.id,
            shared_with_user_id=test_user2.id,
            permission_level="view",
        )
        db_session.add(share)
        await db_session.commit()
        await db_session.refresh(share)

    # Update permission to edit
    async with bypass_rls(db_session):
        share.permission_level = "edit"
        await db_session.commit()
        await db_session.refresh(share)

    assert share.permission_level == "edit"


@pytest.mark.asyncio
async def test_list_shared_with_me_jobs(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test listing jobs shared with a user."""
    # Create multiple jobs and share some with test_user2
    async with bypass_rls(db_session):
        job1 = Job(
            owner_id=test_user.id,
            title="Shared Job 1",
            description="First shared job",
            status="completed",
        )
        job2 = Job(
            owner_id=test_user.id,
            title="Shared Job 2",
            description="Second shared job",
            status="running",
        )
        job3 = Job(
            owner_id=test_user.id,
            title="Private Job",
            description="Not shared",
            status="pending",
        )

        db_session.add_all([job1, job2, job3])
        await db_session.commit()

    # Share job1 and job2 with test_user2
    async with bypass_rls(db_session):
        share1 = JobShare(
            job_id=job1.id, shared_with_user_id=test_user2.id, permission_level="view"
        )
        share2 = JobShare(
            job_id=job2.id, shared_with_user_id=test_user2.id, permission_level="edit"
        )

        db_session.add_all([share1, share2])
        await db_session.commit()

    # Query jobs accessible to test_user2 (RLS will include shared jobs)
    await set_current_user(db_session, test_user2.id)
    stmt = select(Job).order_by(Job.created_at)
    result = await db_session.execute(stmt)
    accessible_jobs = result.scalars().all()

    # Should see job1 and job2, but not job3
    assert len(accessible_jobs) == 2
    accessible_titles = {job.title for job in accessible_jobs}
    assert accessible_titles == {"Shared Job 1", "Shared Job 2"}


@pytest.mark.asyncio
async def test_owner_always_has_access(
    db_session: AsyncSession,
    test_user: User,
):
    """Test that job owners always have access regardless of shares."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Owner Job",
            description="Owner always has access",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Access as owner
    await set_current_user(db_session, test_user.id)
    stmt = select(Job).where(Job.id == job.id)
    result = await db_session.execute(stmt)
    owner_job = result.scalar_one()

    assert owner_job is not None
    assert owner_job.owner_id == test_user.id


@pytest.mark.asyncio
async def test_cannot_share_same_job_twice_to_same_user(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test that duplicate shares are prevented."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Single Share Job",
            description="Test duplicate prevention",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Create first share
    async with bypass_rls(db_session):
        share1 = JobShare(
            job_id=job.id,
            shared_with_user_id=test_user2.id,
            permission_level="view",
        )
        db_session.add(share1)
        await db_session.commit()

    # Try to create duplicate share (should raise integrity error)
    async with bypass_rls(db_session):
        share2 = JobShare(
            job_id=job.id,
            shared_with_user_id=test_user2.id,
            permission_level="edit",
        )
        db_session.add(share2)

        with pytest.raises(Exception):  # IntegrityError or similar
            await db_session.commit()

        await db_session.rollback()


@pytest.mark.asyncio
async def test_cascade_delete_shares_with_job(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test that deleting a job deletes its shares."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Job to Delete",
            description="Will be deleted",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Share the job
    async with bypass_rls(db_session):
        share = JobShare(
            job_id=job.id,
            shared_with_user_id=test_user2.id,
            permission_level="view",
        )
        db_session.add(share)
        await db_session.commit()

    job_id = job.id

    # Delete job
    async with bypass_rls(db_session):
        await db_session.delete(job)
        await db_session.commit()

    # Verify share is also deleted
    async with bypass_rls(db_session):
        stmt = select(JobShare).where(JobShare.job_id == job_id)
        result = await db_session.execute(stmt)
        shares = result.scalars().all()

    assert len(shares) == 0


@pytest.mark.asyncio
async def test_orphaned_job_visibility(db_session: AsyncSession, test_user: User):
    """Test that orphaned jobs (owner_id=NULL) have special visibility rules."""
    # Create orphaned job
    async with bypass_rls(db_session):
        orphaned_job = Job(
            owner_id=None,
            title="Orphaned Job",
            description="No owner",
            status="completed",
        )
        db_session.add(orphaned_job)
        await db_session.commit()
        await db_session.refresh(orphaned_job)

    # According to RLS policies, orphaned jobs should be visible to all users
    await set_current_user(db_session, test_user.id)
    stmt = select(Job).where(Job.id == orphaned_job.id)
    result = await db_session.execute(stmt)
    visible_job = result.scalar_one_or_none()

    # This depends on RLS implementation - check actual policy
    # If orphaned jobs are visible: assert visible_job is not None
    # If orphaned jobs are hidden: assert visible_job is None
    # Based on implementation plan, orphaned jobs should be visible for claiming
    assert visible_job is not None
    assert visible_job.owner_id is None

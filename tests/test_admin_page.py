"""
Tests for admin page functionality.

Tests orphaned job management, user assignment, and job claiming.
"""

from datetime import UTC
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.webapp_components.pages.admin import (
    _filter_users_for_admin_table,
    admin_page,
    delete_user,
    set_user_approval_status,
)
from tests.helpers import fake_admin_session


@pytest.mark.asyncio
async def test_list_orphaned_jobs(db_session: AsyncSession, test_user: User):
    """Test listing orphaned jobs (owner_id=NULL)."""
    # Create orphaned jobs
    orphaned_job1 = Job(
        owner_id=None,
        title="Orphaned Job 1",
        description="No owner",
        status="completed",
    )
    orphaned_job2 = Job(
        owner_id=None,
        title="Orphaned Job 2",
        description="No owner",
        status="failed",
    )

    # Create owned job for comparison
    owned_job = Job(
        owner_id=test_user.id,
        title="Owned Job",
        description="Has owner",
        status="running",
    )

    db_session.add_all([orphaned_job1, orphaned_job2, owned_job])
    await db_session.commit()

    # Query orphaned jobs only
    stmt = select(Job).where(Job.owner_id.is_(None))
    result = await db_session.execute(stmt)
    orphaned_jobs = result.scalars().all()

    assert len(orphaned_jobs) == 2
    assert all(job.owner_id is None for job in orphaned_jobs)
    orphaned_titles = {job.title for job in orphaned_jobs}
    assert orphaned_titles == {"Orphaned Job 1", "Orphaned Job 2"}


@pytest.mark.asyncio
async def test_assign_orphaned_job_to_user(db_session: AsyncSession, test_user: User):
    """Test assigning an orphaned job to a user."""
    # Create orphaned job
    orphaned_job = Job(
        owner_id=None,
        title="Job to Assign",
        description="Will be assigned",
        status="completed",
    )
    db_session.add(orphaned_job)
    await db_session.commit()
    await db_session.refresh(orphaned_job)

    # Assign to user
    orphaned_job.owner_id = test_user.id
    await db_session.commit()
    await db_session.refresh(orphaned_job)

    # Verify assignment
    assert orphaned_job.owner_id == test_user.id

    # Verify user can now access the job via RLS
    await set_current_user(db_session, test_user.id)
    stmt = select(Job).where(Job.id == orphaned_job.id)
    result = await db_session.execute(stmt)
    accessible_job = result.scalar_one()

    assert accessible_job.id == orphaned_job.id


@pytest.mark.asyncio
async def test_user_claims_orphaned_job(db_session: AsyncSession, test_user: User):
    """Test a user claiming an orphaned job."""
    # Create orphaned job
    orphaned_job = Job(
        owner_id=None,
        title="Job to Claim",
        description="Available for claiming",
        status="completed",
    )
    db_session.add(orphaned_job)
    await db_session.commit()
    await db_session.refresh(orphaned_job)

    job_id = orphaned_job.id

    # User claims the job
    stmt = select(Job).where(Job.id == job_id)
    result = await db_session.execute(stmt)
    job_to_claim = result.scalar_one()

    job_to_claim.owner_id = test_user.id
    await db_session.commit()

    # Verify claim
    stmt = select(Job).where(Job.id == job_id)
    result = await db_session.execute(stmt)
    claimed_job = result.scalar_one()

    assert claimed_job.owner_id == test_user.id


@pytest.mark.asyncio
async def test_search_orphaned_jobs_by_title(db_session: AsyncSession):
    """Test searching orphaned jobs by title."""
    # Create orphaned jobs with different titles
    jobs = [
        Job(
            owner_id=None,
            title="Protein Structure Analysis",
            description="Structure",
            status="completed",
        ),
        Job(
            owner_id=None,
            title="DNA Sequencing Study",
            description="Sequencing",
            status="completed",
        ),
        Job(
            owner_id=None,
            title="Protein Binding Analysis",
            description="Binding",
            status="completed",
        ),
    ]

    db_session.add_all(jobs)
    await db_session.commit()

    # Search for "Protein"
    stmt = select(Job).where(
        Job.owner_id.is_(None),
        Job.title.ilike("%Protein%"),
    )
    result = await db_session.execute(stmt)
    protein_jobs = result.scalars().all()

    assert len(protein_jobs) == 2
    titles = {job.title for job in protein_jobs}
    assert titles == {"Protein Structure Analysis", "Protein Binding Analysis"}


@pytest.mark.asyncio
async def test_list_all_users(db_session: AsyncSession, test_user: User, test_user2: User):
    """Test listing all users for assignment purposes."""
    _ = (test_user, test_user2)
    # Query all users
    stmt = select(User).order_by(User.email)
    result = await db_session.execute(stmt)
    users = result.scalars().all()

    assert len(users) >= 2
    emails = {user.email for user in users}
    assert "test@example.com" in emails
    assert "test2@example.com" in emails


@pytest.mark.asyncio
async def test_search_users_by_email(db_session: AsyncSession):
    """Test searching users by email for assignment."""
    # Create users with various emails
    users = [
        User(email="alice@example.com", name="Alice"),
        User(email="bob@example.com", name="Bob"),
        User(email="charlie@different.com", name="Charlie"),
    ]

    db_session.add_all(users)
    await db_session.commit()

    # Search for "@example.com"
    stmt = select(User).where(User.email.ilike("%@example.com%"))
    result = await db_session.execute(stmt)
    example_users = result.scalars().all()

    emails = {user.email for user in example_users}
    assert {"alice@example.com", "bob@example.com"}.issubset(emails)
    assert "charlie@different.com" not in emails


@pytest.mark.asyncio
async def test_search_users_by_name(db_session: AsyncSession):
    """Test searching users by name for assignment."""
    users = [
        User(email="user1@test.com", name="Dr. Jane Smith"),
        User(email="user2@test.com", name="Dr. John Smith"),
        User(email="user3@test.com", name="Dr. Emily Johnson"),
    ]

    db_session.add_all(users)
    await db_session.commit()

    # Search for "Smith"
    stmt = select(User).where(User.name.ilike("%Smith%"))
    result = await db_session.execute(stmt)
    smith_users = result.scalars().all()

    assert len(smith_users) == 2
    names = {user.name for user in smith_users}
    assert names == {"Dr. Jane Smith", "Dr. John Smith"}


@pytest.mark.asyncio
async def test_assign_multiple_orphaned_jobs_to_same_user(
    db_session: AsyncSession,
    test_user: User,
):
    """Test assigning multiple orphaned jobs to the same user."""
    # Create multiple orphaned jobs
    jobs = [
        Job(
            owner_id=None,
            title=f"Orphaned Job {i}",
            description="No owner",
            status="completed",
        )
        for i in range(5)
    ]

    db_session.add_all(jobs)
    await db_session.commit()

    # Assign all to test_user
    for job in jobs:
        await db_session.refresh(job)
        job.owner_id = test_user.id
    await db_session.commit()

    # Verify all assigned
    await set_current_user(db_session, test_user.id)
    stmt = select(Job).where(Job.owner_id == test_user.id)
    result = await db_session.execute(stmt)
    user_jobs = result.scalars().all()

    assert len(user_jobs) >= 5  # At least the 5 we just assigned


@pytest.mark.asyncio
async def test_cannot_assign_already_owned_job(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test that jobs with owners cannot be reassigned as orphaned."""
    _ = test_user2
    # Create job owned by test_user
    owned_job = Job(
        owner_id=test_user.id,
        title="Already Owned",
        description="Has an owner",
        status="completed",
    )
    db_session.add(owned_job)
    await db_session.commit()
    await db_session.refresh(owned_job)

    # Verify it's not in orphaned list
    stmt = select(Job).where(Job.owner_id.is_(None))
    result = await db_session.execute(stmt)
    orphaned_jobs = result.scalars().all()

    orphaned_ids = {job.id for job in orphaned_jobs}
    assert owned_job.id not in orphaned_ids


@pytest.mark.asyncio
async def test_orphaned_job_count(db_session: AsyncSession, test_user: User):
    """Test getting count of orphaned jobs."""
    # Create mix of orphaned and owned jobs
    for i in range(3):
        orphaned = Job(
            owner_id=None,
            title=f"Orphaned {i}",
            description="No owner",
            status="pending",
        )
        db_session.add(orphaned)

    for i in range(2):
        owned = Job(
            owner_id=test_user.id,
            title=f"Owned {i}",
            description="Has owner",
            status="running",
        )
        db_session.add(owned)

    await db_session.commit()

    # Count orphaned jobs
    stmt = select(Job).where(Job.owner_id.is_(None))
    result = await db_session.execute(stmt)
    orphaned_jobs = result.scalars().all()

    assert len(orphaned_jobs) == 3


@pytest.mark.asyncio
async def test_filter_orphaned_jobs_by_status(db_session: AsyncSession):
    """Test filtering orphaned jobs by status."""
    # Create orphaned jobs with different statuses
    statuses = ["pending", "running", "completed", "failed"]
    for status in statuses:
        job = Job(
            owner_id=None,
            title=f"Job {status}",
            description="Test",
            status=status,
        )
        db_session.add(job)

    await db_session.commit()

    # Filter for completed jobs
    stmt = select(Job).where(
        Job.owner_id.is_(None),
        Job.status == "completed",
    )
    result = await db_session.execute(stmt)
    completed_jobs = result.scalars().all()

    assert len(completed_jobs) == 1
    assert completed_jobs[0].status == "completed"


@pytest.mark.asyncio
async def test_orphaned_jobs_sorted_by_creation(db_session: AsyncSession):
    """Test that orphaned jobs are sorted by creation date."""
    from datetime import datetime, timedelta

    base_time = datetime.now(UTC)

    # Create jobs with explicit timestamps to ensure proper ordering
    jobs = []
    for i in range(3):
        job = Job(
            owner_id=None,
            title=f"Job {i}",
            description="Test",
            status="pending",
            # Set explicit timestamps - Job 0 oldest, Job 2 newest
            created_at=base_time + timedelta(hours=i),
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        jobs.append(job)

    # Query sorted by creation date descending (newest first)
    stmt = select(Job).where(Job.owner_id.is_(None)).order_by(Job.created_at.desc())
    result = await db_session.execute(stmt)
    sorted_jobs = result.scalars().all()

    # Newest should be first
    assert sorted_jobs[0].title == "Job 2"
    assert sorted_jobs[-1].title == "Job 0"


@pytest.mark.asyncio
async def test_set_user_approval_status_can_remove_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Admin helper can remove approval from an approved user."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    user = User(
        email="approved@example.com",
        name="Approved User",
        is_approved=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    success, message = await set_user_approval_status(user.id, is_approved=False)

    assert success is True
    assert message == "User approval removed successfully"

    await db_session.refresh(user)
    assert user.is_approved is False


@pytest.mark.asyncio
async def test_set_user_approval_status_rejects_self_unapprove(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Admin helper should reject removing approval from the current user."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    user = User(
        email="self@example.com",
        name="Self User",
        is_approved=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_current_user_id",
        lambda: str(user.id),
    )

    success, message = await set_user_approval_status(user.id, is_approved=False)

    assert success is False
    assert message == "You cannot remove your own approval"

    await db_session.refresh(user)
    assert user.is_approved is True


@pytest.mark.asyncio
async def test_set_user_approval_status_noop_when_already_pending(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Admin helper should report noop when user is already pending."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    user = User(
        email="pending@example.com",
        name="Pending User",
        is_approved=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    success, message = await set_user_approval_status(user.id, is_approved=False)

    assert success is False
    assert message == "User is already pending"


@pytest.mark.asyncio
async def test_set_user_approval_status_handles_missing_user(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Admin helper should return not found for unknown user IDs."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    success, message = await set_user_approval_status(uuid4(), is_approved=False)

    assert success is False
    assert message == "User not found"


def test_filter_users_for_admin_table_hides_current_user():
    """Users table data should not include the currently logged-in admin."""
    current_user = User(
        id=uuid4(),
        email="current@example.com",
        name="Current Admin",
    )
    other_user = User(
        id=uuid4(),
        email="other@example.com",
        name="Other User",
    )

    filtered = _filter_users_for_admin_table(
        [current_user, other_user],
        current_user_id=str(current_user.id),
    )

    assert [user.id for user in filtered] == [other_user.id]


def test_admin_page_defaults_to_users_tab():
    """Admin page should open on Users tab for quick approvals."""
    import inspect

    source = inspect.getsource(admin_page)
    assert 'with ui.tab_panels(tabs, value=users_tab).classes("w-full"):' in source


@pytest.mark.asyncio
async def test_delete_user_removes_user(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """delete_user should remove the user from the database."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    user = User(email="deleteme@example.com", name="Delete Me")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    success, message = await delete_user(user.id)

    assert success is True
    assert message == "User deleted successfully"

    result = await db_session.execute(select(User).where(User.id == user.id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_user_rejects_self_delete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """delete_user should prevent admins from deleting themselves."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    user = User(email="selfadmin@example.com", name="Self Admin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_current_user_id",
        lambda: str(user.id),
    )

    success, message = await delete_user(user.id)

    assert success is False
    assert message == "You cannot delete your own account"

    result = await db_session.execute(select(User).where(User.id == user.id))
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_delete_user_handles_missing_user(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """delete_user should return not found for unknown user IDs."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    success, message = await delete_user(uuid4())

    assert success is False
    assert message == "User not found"


@pytest.mark.asyncio
async def test_delete_user_orphans_jobs(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """Deleting a user should orphan their jobs (SET NULL on owner_id)."""
    monkeypatch.setattr(
        "openscientist.webapp_components.pages.admin.get_admin_session",
        fake_admin_session(db_session),
    )

    user = User(email="jobowner@example.com", name="Job Owner")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    job = Job(
        owner_id=user.id,
        title="User's Job",
        description="Should become orphaned",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    job_id = job.id

    success, _ = await delete_user(user.id)
    assert success is True

    result = await db_session.execute(select(Job).where(Job.id == job_id))
    orphaned_job = result.scalar_one()
    assert orphaned_job.owner_id is None

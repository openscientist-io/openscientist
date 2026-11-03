"""End-to-end unit tests for orphan migration and assignment."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.admin.orphan_jobs import assign_orphaned_job
from openscientist.database.models import Job, User


@pytest.mark.asyncio
async def test_assign_orphaned_job_rejects_already_owned(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Assignment helper should not reassign jobs that already have an owner."""
    owned_job = Job(
        owner_id=test_user.id,
        research_question="Already owned",
        status="completed",
    )
    db_session.add(owned_job)
    await db_session.commit()
    await db_session.refresh(owned_job)

    result = await assign_orphaned_job(
        session=db_session,
        job_id=owned_job.id,
        user_id=test_user2.id,
    )
    assert result.ok is False
    assert result.reason == "already_owned"

    await db_session.refresh(owned_job)
    assert owned_job.owner_id == test_user.id


@pytest.mark.asyncio
async def test_assign_orphaned_job_handles_missing_job(
    db_session: AsyncSession,
    test_user: User,
):
    """Assignment helper should report missing jobs."""
    result = await assign_orphaned_job(
        session=db_session,
        job_id=uuid4(),
        user_id=test_user.id,
    )
    assert result.ok is False
    assert result.reason == "job_not_found"


@pytest.mark.asyncio
async def test_assign_orphaned_job_handles_missing_user(
    db_session: AsyncSession,
):
    """Assignment helper should report missing users before changing ownership."""
    orphan_job = Job(
        owner_id=None,
        research_question="Missing user check",
        status="completed",
    )
    db_session.add(orphan_job)
    await db_session.commit()
    await db_session.refresh(orphan_job)

    result = await assign_orphaned_job(
        session=db_session,
        job_id=orphan_job.id,
        user_id=uuid4(),
    )
    assert result.ok is False
    assert result.reason == "user_not_found"

    await db_session.refresh(orphan_job)
    assert orphan_job.owner_id is None

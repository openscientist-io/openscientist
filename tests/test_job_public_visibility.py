"""
Tests for public job visibility (issue #296).

A job owner can make a job readable by any signed-in user. New jobs are
private by default.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, User


@pytest.mark.asyncio
async def test_new_jobs_are_private_by_default(db_session: AsyncSession, test_user: User):
    job = Job(owner_id=test_user.id, research_question="Default", status="pending")
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.is_public is False

"""
Tests for public job visibility (issue #296).

A job owner can make a job readable by any signed-in user. Row-level security
admits the job and its analysis rows, but not its uploaded inputs, chat or
costs, and only the owner may change the flag.
"""

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import (
    CostRecord,
    Finding,
    Job,
    JobChatMessage,
    JobDataFile,
    JobShare,
    User,
)
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls


async def _job(db_session: AsyncSession, owner: User, *, is_public: bool) -> Job:
    job = Job(
        owner_id=owner.id,
        research_question="Public visibility job",
        description="visibility test",
        status="completed",
        is_public=is_public,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


async def _visible_job(db_session: AsyncSession, job: Job) -> Job | None:
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    return result.scalar_one_or_none()


@pytest.mark.asyncio
async def test_new_jobs_are_private_by_default(db_session: AsyncSession, test_user: User):
    job = Job(owner_id=test_user.id, research_question="Default", status="pending")
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.is_public is False


@pytest.mark.asyncio
async def test_public_job_is_readable_by_any_signed_in_user(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    public_job = await _job(db_session, test_user, is_public=True)
    private_job = await _job(db_session, test_user, is_public=False)
    await enable_rls(db_session)

    await set_current_user(db_session, test_user2.id)

    assert await _visible_job(db_session, public_job) is not None
    assert await _visible_job(db_session, private_job) is None


@pytest.mark.asyncio
async def test_public_job_is_not_readable_without_a_signed_in_user(
    db_session: AsyncSession, test_user: User
):
    public_job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)

    await set_current_user(db_session, None)
    assert await _visible_job(db_session, public_job) is None

    await db_session.execute(text("SELECT set_config('app.current_user_id', '', false)"))
    assert await _visible_job(db_session, public_job) is None


@pytest.mark.asyncio
async def test_public_job_exposes_analysis_rows_but_not_inputs_chat_or_costs(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    db_session.add_all(
        [
            Finding(
                job_id=job.id,
                iteration=1,
                text="Astrocyte markers are reduced",
                finding_type="observation",
                source="analysis",
            ),
            JobDataFile(
                job_id=job.id,
                filename="cohort.csv",
                file_path="data/cohort.csv",
                file_type="csv",
                file_size=10,
            ),
            JobChatMessage(job_id=job.id, role="user", content="private note"),
            CostRecord(
                job_id=job.id,
                operation_type="agent",
                provider="anthropic",
                model="test-model",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.01,
            ),
        ]
    )
    await db_session.commit()
    await enable_rls(db_session)

    await set_current_user(db_session, test_user2.id)

    findings = (await db_session.execute(select(Finding).where(Finding.job_id == job.id))).all()
    assert len(findings) == 1
    for model in (JobDataFile, JobChatMessage, CostRecord):
        rows = (await db_session.execute(select(model).where(model.job_id == job.id))).all()
        assert rows == [], f"{model.__name__} rows leaked from a public job"

    await set_current_user(db_session, test_user.id)
    for model in (JobDataFile, JobChatMessage, CostRecord):
        rows = (await db_session.execute(select(model).where(model.job_id == job.id))).all()
        assert len(rows) == 1, f"owner lost access to {model.__name__}"


@pytest.mark.asyncio
async def test_public_job_is_read_only_for_other_users(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)
    await set_current_user(db_session, test_user2.id)

    await db_session.execute(
        update(Job).where(Job.id == job.id).values(research_question="changed")
    )

    await set_current_user(db_session, test_user.id)
    stored = await db_session.execute(select(Job.research_question).where(Job.id == job.id))
    assert stored.scalar_one() == "Public visibility job"


@pytest.mark.asyncio
async def test_owner_can_toggle_is_public(db_session: AsyncSession, test_user: User):
    job = await _job(db_session, test_user, is_public=False)
    await enable_rls(db_session)
    await set_current_user(db_session, test_user.id)

    await db_session.execute(update(Job).where(Job.id == job.id).values(is_public=True))
    stored = await db_session.execute(select(Job.is_public).where(Job.id == job.id))

    assert stored.scalar_one() is True


@pytest.mark.asyncio
async def test_edit_share_cannot_make_a_job_public(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    """RLS lets an edit share UPDATE jobs; the trigger keeps is_public owner-only."""
    job = await _job(db_session, test_user, is_public=False)
    db_session.add(
        JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="edit")
    )
    await db_session.commit()
    await enable_rls(db_session)
    await set_current_user(db_session, test_user2.id)

    # A savepoint keeps the refused statement from aborting the fixture's transaction.
    with pytest.raises(DBAPIError, match="only the job owner can change is_public"):
        async with db_session.begin_nested():
            await db_session.execute(update(Job).where(Job.id == job.id).values(is_public=True))

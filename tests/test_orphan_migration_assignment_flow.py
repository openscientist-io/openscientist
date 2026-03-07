"""End-to-end unit tests for orphan migration and assignment."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.admin.orphan_jobs import assign_orphaned_job, list_orphaned_jobs
from openscientist.bootstrap import bootstrap_jobs_from_filesystem
from openscientist.database.models import AnalysisLog, Finding, IterationSummary, Job, User
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls, fake_admin_session


def _write_json(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


@pytest.mark.asyncio
async def test_bootstrap_orphan_then_assign_changes_visibility(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    """Legacy filesystem jobs should migrate as orphaned and then be claimable."""
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    job_id = str(uuid4())
    job_dir = temp_jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        job_dir / "config.json",
        {
            "job_id": job_id,
            "research_question": "Orphan migration assignment flow",
            "status": "completed",
            "owner_id": None,
        },
    )
    _write_json(
        job_dir / "knowledge_state.json",
        {
            "config": {
                "job_id": job_id,
                "research_question": "Orphan migration assignment flow",
                "max_iterations": 3,
            },
            "iteration": 1,
            "analysis_log": [
                {
                    "iteration": 1,
                    "action": "execute_code",
                    "timestamp": "2026-03-01T10:00:00",
                    "code": "print('orphan')",
                    "output": "orphan",
                }
            ],
            "findings": [
                {
                    "id": "F001",
                    "iteration_discovered": 1,
                    "title": "Orphan finding",
                    "evidence": "Evidence",
                    "supporting_hypotheses": [],
                    "literature_support": [],
                    "plots": [],
                }
            ],
            "iteration_summaries": [{"iteration": 1, "summary": "Orphan iteration summary"}],
        },
    )

    bootstrap_result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)
    assert bootstrap_result.created_jobs == 1
    assert bootstrap_result.orphan_jobs == 1
    assert bootstrap_result.synced_knowledge_state == 1
    assert bootstrap_result.deleted_knowledge_state_files == 1
    assert bootstrap_result.errors == []

    job = (await db_session.execute(select(Job).where(Job.id == UUID(job_id)))).scalar_one()
    assert job.owner_id is None

    # Verify KS data was migrated into DB tables
    assert (
        await db_session.execute(
            select(func.count(AnalysisLog.id)).where(AnalysisLog.job_id == job.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(select(func.count(Finding.id)).where(Finding.job_id == job.id))
    ).scalar_one() == 1
    assert (
        await db_session.execute(
            select(func.count(IterationSummary.id)).where(IterationSummary.job_id == job.id)
        )
    ).scalar_one() == 1

    # knowledge_state.json should have been deleted after migration
    assert not (job_dir / "knowledge_state.json").exists()

    owner_user = User(email="owner-flow@example.com", name="Owner User")
    other_user = User(email="other-flow@example.com", name="Other User")
    db_session.add_all([owner_user, other_user])
    await db_session.commit()
    await db_session.refresh(owner_user)
    await db_session.refresh(other_user)

    assign_result = await assign_orphaned_job(
        session=db_session,
        job_id=job.id,
        user_id=owner_user.id,
    )
    assert assign_result.ok is True
    assert assign_result.reason == "assigned"

    await db_session.refresh(job)
    assert job.owner_id == owner_user.id

    orphaned_jobs = await list_orphaned_jobs(db_session)
    orphaned_job_ids = {orphan.id for orphan in orphaned_jobs}
    assert job.id not in orphaned_job_ids

    job_uuid = job.id
    owner_user_id = owner_user.id
    other_user_id = other_user.id

    # Verify migrated data survives assignment
    assert (
        await db_session.execute(
            select(func.count(AnalysisLog.id)).where(AnalysisLog.job_id == job_uuid)
        )
    ).scalar_one() == 1

    await enable_rls(db_session)
    await set_current_user(db_session, owner_user_id)
    db_session.expire_all()
    owner_visible = (
        await db_session.execute(select(Job).where(Job.id == job_uuid))
    ).scalar_one_or_none()
    assert owner_visible is not None

    await set_current_user(db_session, other_user_id)
    db_session.expire_all()
    other_visible = (
        await db_session.execute(select(Job).where(Job.id == job_uuid))
    ).scalar_one_or_none()
    assert other_visible is None


@pytest.mark.asyncio
async def test_assign_orphaned_job_rejects_already_owned(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Assignment helper should not reassign jobs that already have an owner."""
    owned_job = Job(
        owner_id=test_user.id,
        title="Already owned",
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
        title="Missing user check",
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

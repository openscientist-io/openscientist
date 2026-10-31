"""The queue picker serves the owner whose jobs held a slot for the fewest hours
in the scheduler window, then that owner's oldest queued job."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job, User
from openscientist.job_manager import JobManager, JobStatus, _db_update_job_status
from openscientist.orchestrator.discovery import (
    _begin_report_phase,
    _persist_final_status,
    _ReportOutcome,
)

_NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _no_knowledge_state_progress() -> Iterator[None]:
    with patch(
        "openscientist.job_manager._load_progress_from_knowledge_state", return_value=(0, 0)
    ):
        yield


@dataclass(frozen=True)
class _Owners:
    alice: UUID
    bob: UUID


@pytest_asyncio.fixture
async def owners(test_engine: AsyncEngine) -> AsyncGenerator[_Owners, None]:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        # Queued rows left by other tests would compete for the slot.
        await session.execute(delete(Job).where(Job.status == "queued"))
        alice = User(email=f"alice-{uuid4().hex}@sched.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@sched.test", name="Bob")
        session.add_all([alice, bob])
        await session.commit()
    yield _Owners(alice=alice.id, bob=bob.id)
    async with AsyncSession(test_engine) as session:
        await session.execute(delete(Job).where(Job.owner_id.in_([alice.id, bob.id])))
        await session.execute(delete(User).where(User.id.in_([alice.id, bob.id])))
        await session.commit()


async def _job(
    test_engine: AsyncEngine,
    owner: UUID,
    status: JobStatus,
    *,
    created: datetime = _NOW,
    started: datetime | None = None,
    ended: datetime | None = None,
) -> UUID:
    job = Job(owner_id=owner, research_question="Q?", status=status.value)
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        session.add(job)
        await session.flush()
        await session.execute(
            text(
                "UPDATE jobs SET created_at = :created, started_at = :started, ended_at = :ended"
                " WHERE id = :id"
            ),
            {"created": created, "started": started, "ended": ended, "id": job.id},
        )
        await session.commit()
    return job.id


async def _picked(tmp_path: Path, window: timedelta = timedelta(hours=24)) -> set[str]:
    manager = JobManager(jobs_dir=tmp_path, scheduler_window=window)
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager._start_next_queued_job)
    return set(manager._running_jobs)


async def test_the_owner_with_fewer_hours_goes_first_even_with_a_newer_job(
    owners: _Owners, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    await _job(
        test_engine,
        owners.alice,
        JobStatus.COMPLETED,
        started=_NOW - timedelta(hours=3),
        ended=_NOW - timedelta(hours=1),
    )
    await _job(
        test_engine,
        owners.bob,
        JobStatus.COMPLETED,
        started=_NOW - timedelta(minutes=20),
        ended=_NOW - timedelta(minutes=10),
    )
    await _job(test_engine, owners.alice, JobStatus.QUEUED, created=_NOW - timedelta(hours=2))
    bobs = await _job(
        test_engine, owners.bob, JobStatus.QUEUED, created=_NOW - timedelta(minutes=5)
    )
    assert await _picked(tmp_path) == {str(bobs)}


async def test_a_running_job_counts_up_to_now(
    owners: _Owners, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    await _job(test_engine, owners.alice, JobStatus.RUNNING, started=_NOW - timedelta(hours=3))
    await _job(
        test_engine,
        owners.bob,
        JobStatus.COMPLETED,
        started=_NOW - timedelta(hours=2),
        ended=_NOW - timedelta(hours=1),
    )
    await _job(test_engine, owners.alice, JobStatus.QUEUED, created=_NOW - timedelta(hours=1))
    bobs = await _job(test_engine, owners.bob, JobStatus.QUEUED)
    manager = JobManager(jobs_dir=tmp_path, max_concurrent=2)
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager._start_next_queued_job)
    assert set(manager._running_jobs) == {str(bobs)}


@pytest.mark.parametrize(("window_hours", "expected"), [(24, "alice"), (48, "bob")])
async def test_only_hours_inside_the_window_count(
    owners: _Owners,
    test_engine: AsyncEngine,
    tmp_path: Path,
    window_hours: int,
    expected: str,
) -> None:
    await _job(
        test_engine,
        owners.alice,
        JobStatus.COMPLETED,
        started=_NOW - timedelta(hours=40),
        ended=_NOW - timedelta(hours=30),
    )
    queued = {
        "alice": await _job(
            test_engine, owners.alice, JobStatus.QUEUED, created=_NOW - timedelta(hours=1)
        ),
        "bob": await _job(test_engine, owners.bob, JobStatus.QUEUED),
    }
    assert await _picked(tmp_path, timedelta(hours=window_hours)) == {str(queued[expected])}


async def test_equal_hours_fall_back_to_the_oldest_queued_job(
    owners: _Owners, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    older = await _job(test_engine, owners.bob, JobStatus.QUEUED, created=_NOW - timedelta(hours=2))
    await _job(test_engine, owners.alice, JobStatus.QUEUED, created=_NOW - timedelta(hours=1))
    await _job(test_engine, owners.bob, JobStatus.QUEUED)
    assert await _picked(tmp_path) == {str(older)}


def test_a_failed_pick_starts_no_job(tmp_path: Path) -> None:
    manager = JobManager(jobs_dir=tmp_path)
    with patch(
        "openscientist.job_manager._db_next_queued_job", side_effect=RuntimeError("database down")
    ):
        started = manager._start_next_queued_job()
    assert started is False
    assert manager._running_jobs == {}


async def _times(test_engine: AsyncEngine, job_id: UUID) -> tuple[datetime | None, datetime | None]:
    async with test_engine.connect() as conn:
        row = (
            await conn.execute(select(Job.started_at, Job.ended_at).where(Job.id == job_id))
        ).one()
    return row.started_at, row.ended_at


async def test_the_status_writers_record_when_a_job_holds_a_slot(
    owners: _Owners, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    job = await _job(test_engine, owners.alice, JobStatus.QUEUED, ended=_NOW - timedelta(days=1))
    await _db_update_job_status(
        str(job), JobStatus.RUNNING, allowed_from=JobManager.WAITING_STATUSES
    )
    started, ended = await _times(test_engine, job)
    assert started is not None
    assert ended is None
    await _begin_report_phase(tmp_path / str(job))
    assert (await _times(test_engine, job)) == (started, None)
    await _persist_final_status(tmp_path / str(job), _ReportOutcome(success=True, error=""))
    started_after, ended = await _times(test_engine, job)
    assert started_after == started
    assert ended is not None and ended >= started


async def test_a_cancel_records_the_end_of_a_job(owners: _Owners, test_engine: AsyncEngine) -> None:
    job = await _job(test_engine, owners.alice, JobStatus.RUNNING, started=_NOW)
    await _db_update_job_status(
        str(job), JobStatus.CANCELLED, allowed_from=JobManager.ACTIVE_STATUSES
    )
    assert (await _times(test_engine, job))[1] is not None

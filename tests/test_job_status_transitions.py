"""Job status writes apply only from the statuses the transition table allows
(plans/job-status-transitions.md), on real database rows."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from openscientist import web_app
from openscientist.database.models import Job, User
from openscientist.job_manager import (
    JobInfo,
    JobManager,
    JobStatus,
    JobStatusUpdateResult,
    _db_update_job_status,
)
from openscientist.orchestrator.discovery import _persist_final_status, _ReportOutcome
from openscientist.orchestrator.iteration import update_job_status
from openscientist.webapp_components import ui_components
from openscientist.webapp_components.pages import job_detail


class MakeJob(Protocol):
    async def __call__(self, status: JobStatus, error: str | None = None) -> UUID: ...


@pytest.fixture(autouse=True)
def _no_knowledge_state_progress():
    with patch(
        "openscientist.job_manager._load_progress_from_knowledge_state", return_value=(0, 0)
    ):
        yield


@pytest_asyncio.fixture
async def make_job(test_engine: AsyncEngine) -> AsyncGenerator[MakeJob, None]:
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    created: list[UUID] = []

    async def _make(status: JobStatus, error: str | None = None) -> UUID:
        job = Job(research_question=f"In {status.value}?", status=status.value)
        job.error_message = error
        async with factory() as session:
            session.add(job)
            await session.commit()
        created.append(job.id)
        return job.id

    yield _make
    async with factory() as session:
        await session.execute(delete(Job).where(Job.id.in_(created)))
        await session.commit()


class OwnJob(Protocol):
    async def __call__(self, status: JobStatus) -> tuple[UUID, UUID]: ...


@pytest_asyncio.fixture
async def owned_job(
    test_engine: AsyncEngine,
) -> AsyncGenerator[OwnJob, None]:
    """Committed jobs owned by a committed user, so the user's RLS session sees them."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    created: list[tuple[UUID, UUID]] = []

    async def _make(status: JobStatus) -> tuple[UUID, UUID]:
        async with factory() as session:
            owner = User(email=f"owner-{uuid4().hex}@status.test", name="Owner")
            session.add(owner)
            await session.flush()
            job = Job(owner_id=owner.id, research_question="Does it resume?", status=status.value)
            session.add(job)
            await session.commit()
        created.append((job.id, owner.id))
        return job.id, owner.id

    yield _make
    for job_id, owner_id in created:
        async with factory.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": owner_id})


async def _status(test_engine: AsyncEngine, job_id: UUID) -> str:
    return (await _ending(test_engine, job_id))[0]


async def _ending(test_engine: AsyncEngine, job_id: UUID) -> tuple[str, str | None]:
    async with test_engine.connect() as conn:
        row = (
            await conn.execute(select(Job.status, Job.error_message).where(Job.id == job_id))
        ).one()
    return str(row.status), row.error_message


@pytest.fixture
def runner() -> Iterator[MagicMock]:
    """The container runner, which launches nothing. A launched container
    reports a crash at once, so a worker that should not have launched ends fast."""
    fake = MagicMock()
    fake.get_exit_code.return_value = 1
    with (
        patch("openscientist.job_container.JobContainerRunner", return_value=fake),
        patch("openscientist.job_manager.time.sleep"),
    ):
        yield fake


def _manager(tmp_path: Path, max_concurrent: int = 1) -> JobManager:
    return JobManager(jobs_dir=tmp_path, max_concurrent=max_concurrent)


async def test_building_a_job_manager_changes_no_job(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    """A CLI command or a dev reload builds one while the web's jobs run."""
    running = await make_job(JobStatus.RUNNING)
    _manager(tmp_path)
    assert await _status(test_engine, running) == "running"
    runner.cleanup.assert_not_called()


async def test_a_job_cancelled_before_its_worker_begins_is_not_launched(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    manager = _manager(tmp_path)
    job = await make_job(JobStatus.QUEUED)
    await manager.cancel_job(str(job))
    with patch.object(manager, "_start_next_queued_job"):
        await asyncio.to_thread(manager._run_job_in_container, str(job))
    runner.launch.assert_not_called()
    assert await _status(test_engine, job) == "cancelled"


async def test_a_launch_crash_fails_the_job_and_starts_the_next_one(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock, monkeypatch
) -> None:
    """A crash while launching the container fails the job with that error, frees
    its slot, and starts the next queued job."""
    monkeypatch.setattr("openscientist.version._commit", None)
    manager = _manager(tmp_path, max_concurrent=2)
    crasher = await make_job(JobStatus.QUEUED)
    next_up = await make_job(JobStatus.QUEUED)
    runner.launch.side_effect = RuntimeError("docker launch failed")
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager._run_job_in_container, str(crasher))
    status, error = await _ending(test_engine, crasher)
    assert status == "failed"
    assert "docker launch failed" in (error or "")
    assert str(crasher) not in manager._running_jobs
    assert str(next_up) in manager._running_jobs


async def test_cancelling_a_started_queued_job_keeps_its_slot_and_stops_it(
    make_job: MakeJob, tmp_path: Path, runner: MagicMock
) -> None:
    """The job's thread is live, so it keeps counting against max_concurrent."""
    manager = _manager(tmp_path)
    job = await make_job(JobStatus.QUEUED)
    manager._running_jobs[str(job)] = MagicMock()
    with patch.object(manager, "_start_next_queued_job"):
        await manager.cancel_job(str(job))
    assert str(job) in manager._running_jobs
    runner.stop.assert_called_once_with(str(job))


async def test_a_started_queued_job_is_not_picked_again(
    make_job: MakeJob, tmp_path: Path, runner: MagicMock
) -> None:
    manager = _manager(tmp_path, max_concurrent=2)
    job = await make_job(JobStatus.QUEUED)
    own_thread = MagicMock()
    manager._running_jobs[str(job)] = own_thread
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager._start_next_queued_job)
    assert manager._running_jobs[str(job)] is own_thread


async def test_the_oldest_queued_job_starts_first(
    make_job: MakeJob, tmp_path: Path, runner: MagicMock
) -> None:
    manager = _manager(tmp_path)
    older = await make_job(JobStatus.QUEUED)
    newer = await make_job(JobStatus.QUEUED)
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager._start_next_queued_job)
    assert str(older) in manager._running_jobs
    assert str(newer) not in manager._running_jobs


async def test_a_queued_job_waits_while_a_slot_is_taken(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A full manager leaves queued jobs queued instead of overrunning its limit."""
    manager = _manager(tmp_path, max_concurrent=1)
    job = await make_job(JobStatus.QUEUED)
    manager._running_jobs["busy-job"] = MagicMock()
    assert manager._start_next_queued_job() is False
    assert str(job) not in manager._running_jobs
    assert await _status(test_engine, job) == "queued"


async def test_a_job_waiting_for_feedback_can_be_cancelled(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    manager = _manager(tmp_path)
    job = await make_job(JobStatus.AWAITING_FEEDBACK)
    manager._running_jobs[str(job)] = MagicMock()
    with patch.object(manager, "_start_next_queued_job"):
        await manager.cancel_job(str(job))
    assert await _status(test_engine, job) == "cancelled"
    runner.stop.assert_called_once_with(str(job))


@pytest.mark.parametrize("finished", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
async def test_cancelling_a_finished_job_fails_loudly(
    make_job: MakeJob,
    test_engine: AsyncEngine,
    tmp_path: Path,
    runner: MagicMock,
    finished: JobStatus,
) -> None:
    """An ended job refuses a late cancel and keeps its recorded ending."""
    manager = _manager(tmp_path)
    job = await make_job(finished)
    with pytest.raises(ValueError, match="already ended"):
        await manager.cancel_job(str(job))
    assert await _status(test_engine, job) == finished.value
    runner.stop.assert_not_called()


@pytest.mark.parametrize("finished", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
async def test_start_job_refuses_a_finished_job(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, finished: JobStatus
) -> None:
    manager = _manager(tmp_path)
    job = await make_job(finished)
    with pytest.raises(ValueError, match="cannot be started"):
        await asyncio.to_thread(manager.start_job, str(job))
    assert str(job) not in manager._running_jobs
    assert await _status(test_engine, job) == finished.value


async def test_starting_a_job_that_stopped_waiting_fails_loudly(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A job cancelled right before the queue write is not started, and the error says so."""
    manager = _manager(tmp_path)
    job = await make_job(JobStatus.PENDING)
    manager._running_jobs["busy-job"] = MagicMock()
    with patch(
        "openscientist.job_manager._db_update_job_status",
        new_callable=AsyncMock,
        return_value=JobStatusUpdateResult(),
    ):
        with pytest.raises(ValueError, match="stopped waiting"):
            await asyncio.to_thread(manager.start_job, str(job))
    assert str(job) not in manager._running_jobs
    assert await _status(test_engine, job) == "pending"


@pytest.mark.parametrize(
    ("web_status", "web_error", "outcome"),
    [
        (JobStatus.CANCELLED, None, _ReportOutcome(success=True, error="")),
        (JobStatus.FAILED, "Job timed out", _ReportOutcome(success=False, error="report")),
    ],
)
async def test_the_containers_result_does_not_overwrite_the_webs_ending(
    make_job: MakeJob,
    test_engine: AsyncEngine,
    tmp_path: Path,
    web_status: JobStatus,
    web_error: str | None,
    outcome: _ReportOutcome,
) -> None:
    """A cancel or a web timeout lands before the container's final write."""
    job = await make_job(web_status, web_error)
    await _persist_final_status(tmp_path / str(job), outcome)
    assert await _ending(test_engine, job) == (web_status.value, web_error)


async def test_the_webs_late_failure_does_not_overwrite_a_completed_job(
    make_job: MakeJob, test_engine: AsyncEngine
) -> None:
    """The container finished just before the web's timeout or crash check."""
    job = await make_job(JobStatus.COMPLETED)
    result = await _db_update_job_status(
        str(job),
        JobStatus.FAILED,
        allowed_from=JobManager.STARTED_STATUSES,
        error_message="Job timed out",
    )
    assert not result.applied
    assert await _status(test_engine, job) == "completed"


async def test_a_status_write_that_cannot_reach_the_database_raises(
    make_job: MakeJob, tmp_path: Path
) -> None:
    """A database failure reaches the caller, and the owner is not told about a
    move that never happened."""
    manager = _manager(tmp_path)
    job = await make_job(JobStatus.RUNNING)
    with (
        patch("openscientist.job_manager.run_sync", side_effect=RuntimeError("database down")),
        patch.object(manager, "_send_status_notification") as notify,
        pytest.raises(RuntimeError, match="database down"),
    ):
        manager._update_job_status(
            str(job),
            JobStatus.FAILED,
            allowed_from=JobManager.STARTED_STATUSES,
            error_message="boom",
            retry=False,
        )
    notify.assert_not_called()


async def test_startup_recovery_keeps_waiting_jobs_and_ends_started_ones(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    pending = await make_job(JobStatus.PENDING)
    queued = await make_job(JobStatus.QUEUED)
    running = await make_job(JobStatus.RUNNING)
    paused = await make_job(JobStatus.AWAITING_FEEDBACK)
    completed = await make_job(JobStatus.COMPLETED)
    manager = _manager(tmp_path, max_concurrent=5)
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager.recover_after_restart)
    assert await _status(test_engine, running) == "cancelled"
    assert await _status(test_engine, paused) == "cancelled"
    assert await _status(test_engine, completed) == "completed"
    assert await _status(test_engine, pending) == "queued"
    assert await _status(test_engine, queued) == "queued"
    assert {str(pending), str(queued)} <= manager._running_jobs.keys()
    cleaned = {call.args[0] for call in runner.cleanup.call_args_list}
    assert {str(running), str(paused)} <= cleaned


async def test_a_lost_jobs_cleanup_failure_does_not_stop_recovery(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    """A container whose cleanup crashes is still ended, and the other lost
    jobs are still processed."""
    broken = await make_job(JobStatus.RUNNING)
    other = await make_job(JobStatus.GENERATING_REPORT)

    def _cleanup(job_id: str) -> None:
        if job_id == str(broken):
            raise RuntimeError("docker daemon is down")

    runner.cleanup.side_effect = _cleanup
    manager = _manager(tmp_path, max_concurrent=5)
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager.recover_after_restart)
    assert await _status(test_engine, broken) == "cancelled"
    assert await _status(test_engine, other) == "cancelled"


async def test_the_web_startup_recovers_the_jobs_the_previous_process_lost(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    """The web startup runs the restart recovery, so waiting jobs move forward
    and the jobs whose containers were lost are ended."""
    pending = await make_job(JobStatus.PENDING)
    lost = await make_job(JobStatus.RUNNING)
    previous = web_app._state.job_manager
    try:
        web_app._state.job_manager = None
        with patch.object(JobManager, "_run_job"):
            web_app._initialize_job_manager_runtime(tmp_path)
            # Recovery runs in the background, so startup does not wait on it.
            for thread in threading.enumerate():
                if thread.name == "job-recovery":
                    thread.join(timeout=30)
        manager = web_app._state.job_manager
        assert manager is not None
        assert await _status(test_engine, pending) == "queued"
        assert await _status(test_engine, lost) == "cancelled"
        assert str(pending) in manager._running_jobs
    finally:
        web_app._state.job_manager = previous


# ─── the web surfaces a refused resume ─────────────────────────────────


async def test_submitting_feedback_resumes_a_paused_job(
    owned_job: OwnJob, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The feedback panel flips the job back to running and tells the user it continues."""
    job_id, owner_id = await owned_job(JobStatus.AWAITING_FEEDBACK)
    job_dir = tmp_path / str(job_id)
    job_dir.mkdir()
    with patch.object(job_detail, "ui") as mock_ui:
        await job_detail._submit_feedback_and_continue(
            job_dir, str(job_id), str(owner_id), 1, "check the PDB route"
        )
    assert await _status(test_engine, job_id) == "running"
    mock_ui.notify.assert_called_once_with("Continuing to next iteration", type="positive")
    mock_ui.navigate.to.assert_called_once_with(f"/job/{job_id}")


async def test_feedback_for_a_job_that_ended_while_paused_keeps_its_ending(
    owned_job: OwnJob, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A job that ended while the panel was open is not resumed, and the user is told."""
    job_id, owner_id = await owned_job(JobStatus.CANCELLED)
    job_dir = tmp_path / str(job_id)
    job_dir.mkdir()
    with patch.object(job_detail, "ui") as mock_ui:
        await job_detail._submit_feedback_and_continue(job_dir, str(job_id), str(owner_id), 1, "")
    assert await _status(test_engine, job_id) == "cancelled"
    mock_ui.notify.assert_called_once_with(
        "This job is no longer waiting for feedback.", type="warning"
    )
    mock_ui.navigate.to.assert_called_once_with(f"/job/{job_id}")


def _job_info(status: JobStatus) -> JobInfo:
    return JobInfo(
        job_id=str(uuid4()), research_question="Q?", status=status, created_at="2026-01-01"
    )


@pytest.mark.parametrize(
    ("status", "warns"),
    [
        (JobStatus.RUNNING, True),
        (JobStatus.AWAITING_FEEDBACK, True),
        (JobStatus.COMPLETED, False),
    ],
)
async def test_the_delete_dialog_warns_only_when_the_job_is_active(
    status: JobStatus, warns: bool, tmp_path: Path
) -> None:
    """The dialog warns the user that an active job will be cancelled before deletion."""
    manager = JobManager(jobs_dir=tmp_path)
    with (
        patch.object(manager, "get_job", new_callable=AsyncMock, return_value=_job_info(status)),
        patch.object(ui_components, "ui") as mock_ui,
    ):
        dialog = cast(MagicMock, ui_components.render_delete_dialog(str(uuid4()), manager))
        show = [call for call in dialog.on.call_args_list if call.args and call.args[0] == "show"]
        await show[0].args[1]()
    labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
    active_note = "This job is currently active and will be cancelled before deletion."
    assert (active_note in labels) is warns


@pytest.mark.parametrize(
    ("status", "cancels_first"),
    [
        (JobStatus.RUNNING, True),
        (JobStatus.COMPLETED, False),
    ],
)
async def test_deleting_a_job_cancels_it_only_while_it_is_active(
    status: JobStatus, cancels_first: bool, tmp_path: Path
) -> None:
    """Confirming the delete dialog cancels an active job before deleting it."""
    job_id = str(uuid4())
    owner = uuid4()
    manager = MagicMock()
    manager.ACTIVE_STATUSES = JobManager.ACTIVE_STATUSES
    manager.get_job = AsyncMock(return_value=_job_info(status))
    manager.cancel_job = AsyncMock()
    manager.delete_job = AsyncMock()
    captured: list[Callable[[], Awaitable[None]]] = []

    def _capture_actions(*_args: object, **kwargs: object) -> MagicMock:
        captured.append(cast("Callable[[], Awaitable[None]]", kwargs["on_confirm"]))
        return MagicMock()

    @contextlib.asynccontextmanager
    async def _admin_session():
        yield SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(owner_id=owner)))

    with (
        patch.object(ui_components, "render_dialog_actions", _capture_actions),
        patch.object(ui_components, "get_current_user_id", return_value=str(owner)),
        patch.object(ui_components, "get_admin_session", _admin_session),
        patch.object(ui_components, "ui") as mock_ui,
    ):
        ui_components.render_delete_dialog(job_id, manager)
        await captured[0]()
    if cancels_first:
        manager.cancel_job.assert_awaited_once_with(job_id)
    else:
        manager.cancel_job.assert_not_called()
    manager.delete_job.assert_awaited_once_with(job_id)
    mock_ui.notify.assert_called_once_with(
        f"Job {job_id[-8:]} deleted successfully", type="positive"
    )


async def test_a_job_id_already_in_use_is_refused_and_the_job_kept(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    job = await make_job(JobStatus.COMPLETED)
    with pytest.raises(ValueError, match="already exists"):
        await _manager(tmp_path).create_job(str(job), "Another question?", data_files=[])
    assert await _status(test_engine, job) == "completed"


async def test_listing_jobs_by_status_returns_only_that_status(
    make_job: MakeJob, tmp_path: Path, runner: MagicMock
) -> None:
    queued = await make_job(JobStatus.QUEUED)
    running = await make_job(JobStatus.RUNNING)
    listed = {job.job_id for job in await _manager(tmp_path).list_jobs(status=JobStatus.QUEUED)}
    assert str(queued) in listed
    assert str(running) not in listed


async def test_a_cancel_completes_when_the_container_cannot_be_stopped(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    manager = _manager(tmp_path)
    job = await make_job(JobStatus.RUNNING)
    manager._running_jobs[str(job)] = MagicMock()
    runner.stop.side_effect = RuntimeError("docker is unreachable")
    with patch.object(manager, "_start_next_queued_job"):
        await manager.cancel_job(str(job))
    assert await _status(test_engine, job) == "cancelled"


async def test_a_cancel_completes_when_the_owner_cannot_be_notified(
    test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as session:
        owner = User(
            email=f"ntfy-{uuid4().hex}@cancel.test",
            name="Owner",
            ntfy_enabled=True,
            ntfy_topic=f"topic-{uuid4().hex}",
        )
        session.add(owner)
        await session.flush()
        job = Job(owner_id=owner.id, research_question="Notify me?", status="running")
        session.add(job)
        await session.commit()
    try:
        with (
            patch(
                "openscientist.job_manager.notify_job_status_change",
                side_effect=RuntimeError("ntfy is down"),
            ) as notify,
            patch.object(JobManager, "_start_next_queued_job"),
        ):
            await _manager(tmp_path).cancel_job(str(job.id))
        notify.assert_called_once()
        assert await _status(test_engine, job.id) == "cancelled"
    finally:
        async with factory() as session:
            await session.execute(delete(Job).where(Job.id == job.id))
            await session.execute(delete(User).where(User.id == owner.id))
            await session.commit()


# ─── the agent's status label (plans/next-prs.md, N3 state table) ─────────


async def _label(test_engine: AsyncEngine, job_id: UUID) -> tuple[str | None, object]:
    async with test_engine.connect() as conn:
        row = (
            await conn.execute(
                select(Job.agent_status, Job.agent_status_updated_at).where(Job.id == job_id)
            )
        ).one()
    return row.agent_status, row.agent_status_updated_at


async def _set_label(test_engine: AsyncEngine, job_id: UUID, label: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET agent_status = :label, agent_status_updated_at = now() "
                "WHERE id = :id"
            ),
            {"label": label, "id": job_id},
        )


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.RUNNING, JobStatus.CANCELLED),
        (JobStatus.AWAITING_FEEDBACK, JobStatus.CANCELLED),
    ],
)
async def test_the_web_ending_a_job_clears_its_label(
    make_job: MakeJob, test_engine: AsyncEngine, start: JobStatus, target: JobStatus
) -> None:
    """A crash, a timeout or a cancel ends the job, and the label of the work it
    was doing ends with it."""
    job = await make_job(start)
    await _set_label(test_engine, job, "Running PCA")
    result = await _db_update_job_status(str(job), target, allowed_from=JobManager.ACTIVE_STATUSES)
    assert result.applied
    assert await _label(test_engine, job) == (None, None)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("running", "completed"),
        ("running", "failed"),
        ("awaiting_feedback", "failed"),
        ("running", "awaiting_feedback"),
    ],
)
async def test_the_container_ending_or_pausing_a_job_clears_its_label(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, start: str, target: str
) -> None:
    job = await make_job(JobStatus(start))
    await _set_label(test_engine, job, "Searching PubMed")
    assert await update_job_status(tmp_path / str(job), target, allowed_from=(start,))
    assert await _label(test_engine, job) == (None, None)


async def test_a_refused_status_write_keeps_the_label(
    make_job: MakeJob, test_engine: AsyncEngine
) -> None:
    """The label goes only with a status change that applies."""
    job = await make_job(JobStatus.RUNNING)
    await _set_label(test_engine, job, "Running PCA")
    result = await _db_update_job_status(
        str(job), JobStatus.FAILED, allowed_from=JobManager.WAITING_STATUSES
    )
    assert not result.applied
    assert (await _label(test_engine, job))[0] == "Running PCA"


async def test_a_resumed_job_shows_no_label_from_before_its_pause(
    owned_job: OwnJob, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The job page shows the label while a job runs, so a resumed job must not
    show what the agent was doing before the pause."""
    job_id, owner_id = await owned_job(JobStatus.RUNNING)
    await _set_label(test_engine, job_id, "Writing iteration summary")
    job_dir = tmp_path / str(job_id)
    job_dir.mkdir()
    assert await update_job_status(job_dir, "awaiting_feedback", allowed_from=("running",))
    with patch.object(job_detail, "ui"):
        await job_detail._submit_feedback_and_continue(
            job_dir, str(job_id), str(owner_id), 1, "try the other assay"
        )
    assert await _status(test_engine, job_id) == "running"
    assert await _label(test_engine, job_id) == (None, None)


async def test_a_regenerated_report_shows_no_label_from_the_original_run(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A regeneration runs a completed job again, which must not bring back the
    label the original run ended with."""
    job = await make_job(JobStatus.RUNNING)
    await _set_label(test_engine, job, "Writing the final report")
    await _persist_final_status(tmp_path / str(job), _ReportOutcome(success=True, error=""))
    result = await _db_update_job_status(
        str(job), JobStatus.RUNNING, allowed_from={JobStatus.COMPLETED}
    )
    assert result.applied
    assert await _label(test_engine, job) == (None, None)


async def test_startup_recovery_clears_the_labels_of_the_jobs_it_cancels(
    make_job: MakeJob, test_engine: AsyncEngine, tmp_path: Path, runner: MagicMock
) -> None:
    running = await make_job(JobStatus.RUNNING)
    paused = await make_job(JobStatus.AWAITING_FEEDBACK)
    for job in (running, paused):
        await _set_label(test_engine, job, "Running PCA")
    manager = _manager(tmp_path, max_concurrent=5)
    with patch.object(manager, "_run_job"):
        await asyncio.to_thread(manager.recover_after_restart)
    for job in (running, paused):
        assert await _status(test_engine, job) == "cancelled"
        assert await _label(test_engine, job) == (None, None)

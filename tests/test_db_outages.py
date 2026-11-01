"""Job-state paths under a real database outage (plans/swallowed-db-errors.md),
through the ``database`` forwarder fixture."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import openscientist.database.engine as engine_module
from openscientist.database.models import Job, User
from openscientist.database.retry import DatabaseUnavailableError
from openscientist.job_manager import JobManager, JobStatus
from openscientist.orchestrator.discovery import _assert_job_running
from openscientist.orchestrator.iteration import update_job_status
from tests.admin_row import (
    default_admin_settings_values,
    reset_admin_settings_row,
    set_admin_settings_row,
)
from tests.db_logins import reset_database_state
from tests.db_outage import Forwarder


@pytest.fixture(autouse=True)
def _no_knowledge_state_progress() -> Iterator[None]:
    with patch(
        "openscientist.job_manager._load_progress_from_knowledge_state", return_value=(0, 0)
    ):
        yield


@pytest_asyncio.fixture(autouse=True)
async def admin_settings_row(test_engine: AsyncEngine):
    """The row the slot checks and the queue pick read, with a roomy cap."""
    await set_admin_settings_row(test_engine, max_concurrent_jobs=5)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


@pytest_asyncio.fixture
async def job_in(test_engine: AsyncEngine) -> AsyncGenerator[dict[JobStatus, UUID], None]:
    """One job in each status, created straight on the test database."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(delete(Job).where(Job.status == JobStatus.QUEUED.value))
        jobs = {status: Job(research_question="Q?", status=status.value) for status in JobStatus}
        session.add_all(jobs.values())
        await session.commit()
    yield {status: job.id for status, job in jobs.items()}
    async with factory() as session:
        await session.execute(delete(Job).where(Job.id.in_([job.id for job in jobs.values()])))
        await session.commit()


async def _status(test_engine: AsyncEngine, job_id: UUID) -> str:
    async with test_engine.connect() as conn:
        return str(await conn.scalar(select(Job.status).where(Job.id == job_id)))


async def _down_for(database: Forwarder, seconds: float) -> None:
    await asyncio.sleep(seconds)
    database.up()


def _stop_manager_background(manager: JobManager) -> None:
    """Cancel pending repicks and join the worker threads, so the test
    leaves nothing running that could touch a later test's database."""
    threads: list[threading.Thread] = []
    for _ in range(5):
        with manager._lock:
            timer = manager._repick
            manager._repick = None
            threads.extend(manager._running_jobs.values())
            manager._running_jobs.clear()
        if timer is None:
            break
        timer.cancel()
        time.sleep(0.05)
    for thread in threads:
        thread.join(timeout=5)


async def test_a_container_write_outlives_a_short_outage(
    database: Forwarder, test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    job = job_in[JobStatus.RUNNING]
    database.down()
    write = asyncio.create_task(
        update_job_status(tmp_path / str(job), "completed", allowed_from=("running",))
    )
    await _down_for(database, 2)
    assert await asyncio.wait_for(write, 20)
    assert await _status(test_engine, job) == "completed"


async def test_a_container_write_fails_once_the_outage_outlasts_its_window(
    database: Forwarder, test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    job = job_in[JobStatus.RUNNING]
    database.down()
    started = time.monotonic()
    with pytest.raises(DatabaseUnavailableError):
        await update_job_status(
            tmp_path / str(job), "completed", allowed_from=("running",), retry_for=2
        )
    assert time.monotonic() - started < 10
    database.up()
    assert await _status(test_engine, job) == "running"


async def test_a_container_status_read_outlives_a_short_outage(
    database: Forwarder, job_in: dict[JobStatus, UUID]
) -> None:
    database.down()
    read = asyncio.create_task(_assert_job_running(str(job_in[JobStatus.RUNNING])))
    await _down_for(database, 2)
    await asyncio.wait_for(read, 20)


async def test_any_container_read_waits_out_a_short_outage(
    database: Forwarder,
    job_in: dict[JobStatus, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent containers run with a connect window, so a read that is not a
    status call, like loading the job's runtime context, survives too."""
    from openscientist.database.retry import CONTAINER_DB_RETRY_SECONDS
    from openscientist.orchestrator.discovery import _load_runtime_context

    monkeypatch.setenv("OPENSCIENTIST_DB_CONNECT_RETRY_SECONDS", str(CONTAINER_DB_RETRY_SECONDS))
    reset_database_state()
    database.down()
    read = asyncio.create_task(_load_runtime_context(tmp_path / str(job_in[JobStatus.RUNNING])))
    await _down_for(database, 2)
    context = await asyncio.wait_for(read, 20)
    assert context["research_question"] == "Q?"


async def test_an_error_other_than_a_lost_connection_is_not_retried(
    test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    @asynccontextmanager
    async def read_only() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(test_engine) as session:
            await session.execute(text("SET LOCAL ROLE pg_read_all_data"))
            yield session

    started = time.monotonic()
    with pytest.raises(DBAPIError, match="permission denied"):
        await update_job_status(
            tmp_path / str(job_in[JobStatus.RUNNING]),
            "failed",
            allowed_from=("running",),
            open_session=read_only,
        )
    assert time.monotonic() - started < 1


def test_a_background_write_raises_a_non_connection_error_at_once() -> None:
    """A background write that fails for a reason the retry cannot fix is not
    retried: the error reaches the caller at once, and the write is not made
    again."""
    from openscientist.database.retry import retry_sync

    calls = 0

    def write() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("unique violation")

    started = time.monotonic()
    with pytest.raises(ValueError, match="unique violation"):
        retry_sync(write, what="moving a job")
    assert calls == 1
    assert time.monotonic() - started < 1


async def test_the_admin_engine_reports_an_outage_instead_of_a_connect_error(
    database: Forwarder,
) -> None:
    """The admin engine answers like the web's: a working database connects,
    and an unreachable one answers as a database outage, not as a bare
    connect failure."""
    engine = engine_module.get_admin_engine()
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT 1")) == 1
    database.down()
    with pytest.raises(DatabaseUnavailableError):
        await engine.connect()


async def test_a_job_read_during_an_outage_raises_instead_of_not_found(
    database: Forwarder, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    manager = JobManager(jobs_dir=tmp_path)
    database.down()
    with pytest.raises(DatabaseUnavailableError):
        await manager.get_job(str(job_in[JobStatus.RUNNING]))


async def test_the_api_answers_503_during_an_outage(
    database: Forwarder, job_in: dict[JobStatus, UUID]
) -> None:
    from fastapi import FastAPI

    from openscientist.api.auth import get_current_user_from_api_key
    from openscientist.api.errors import register_database_error_handlers
    from openscientist.api.router import api_router

    user = User(id=uuid4(), email="outage@test.local", name="Outage")
    app = FastAPI()
    app.dependency_overrides[get_current_user_from_api_key] = lambda: user
    app.include_router(api_router)
    register_database_error_handlers(app)
    database.down()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/jobs/{job_in[JobStatus.RUNNING]}")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


async def test_a_database_error_other_than_an_outage_keeps_its_own_answer() -> None:
    """Only a lost connection is answered 503; a database error the retry
    cannot fix, like a syntax error, reaches the caller unchanged."""
    import asyncpg  # type: ignore[import-untyped]
    from fastapi import FastAPI

    from openscientist.api.errors import register_database_error_handlers

    error = DBAPIError("SELECT broken", (), asyncpg.exceptions.PostgresSyntaxError("syntax error"))

    app = FastAPI()
    register_database_error_handlers(app)

    @app.get("/broken")
    async def broken() -> None:
        raise error

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(DBAPIError) as raised:
            await client.get("/broken")
    assert raised.value is error


async def test_registering_the_api_installs_the_outage_handlers() -> None:
    """Registering the API installs the database-outage handlers, so a route
    that loses the database answers 503 through the web app's registration."""
    from fastapi import FastAPI

    import openscientist.web_app as web_app_module
    from openscientist.api import errors as errors_module

    app = FastAPI()
    web_app_module._register_api_routes(app)
    assert app.exception_handlers[DatabaseUnavailableError] is errors_module._database_error
    assert app.exception_handlers[DBAPIError] is errors_module._database_error

    @app.get("/outage")
    async def outage() -> None:
        raise DatabaseUnavailableError("refused")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/outage")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


async def test_cancelling_a_job_that_ends_first_answers_400(
    database: Forwarder, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A cancel that loses the race against the job's own end answers 400 with
    the job manager's error, instead of a 500. The job is running when the
    cancel checks it and ends before the cancel's guarded write, which the
    write reports as not applied."""
    from fastapi import FastAPI

    import openscientist.web_app as web_app_module
    from openscientist.api.auth import get_current_user_from_api_key
    from openscientist.api.router import api_router
    from openscientist.job.types import JobStatusUpdateResult

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as session:
        user = User(id=uuid4(), email="cancel-race@test.local", name="Cancel Race")
        job = Job(owner_id=user.id, research_question="Q?", status=JobStatus.RUNNING.value)
        session.add_all([user, job])
        await session.commit()

    manager = JobManager(jobs_dir=tmp_path)
    saved = web_app_module._state.job_manager
    web_app_module._state.job_manager = manager
    app = FastAPI()
    app.dependency_overrides[get_current_user_from_api_key] = lambda: user
    app.include_router(api_router)
    try:
        with patch(
            "openscientist.job_manager._db_update_job_status",
            new_callable=AsyncMock,
            return_value=JobStatusUpdateResult(applied=False),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(f"/api/v1/jobs/{job.id}/cancel")
        stored = await _status(test_engine, job.id)
    finally:
        web_app_module._state.job_manager = saved
        async with factory() as session:
            await session.execute(delete(Job).where(Job.id == job.id))
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()
    assert response.status_code == 400
    assert "has already ended" in response.json()["detail"]
    assert stored == "running"


async def test_a_worker_write_applies_once_the_database_returns(
    database: Forwarder, test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    manager = JobManager(jobs_dir=tmp_path)
    job = job_in[JobStatus.RUNNING]
    database.down()
    write = asyncio.create_task(
        asyncio.to_thread(
            manager._update_job_status,
            str(job),
            JobStatus.FAILED,
            allowed_from=JobManager.STARTED_STATUSES,
            error_message="Agent container exited with code 1",
            notify=False,
            retry=True,
        )
    )
    await _down_for(database, 2)
    assert await asyncio.wait_for(write, 20)
    assert await _status(test_engine, job) == "failed"


async def test_startup_recovery_runs_once_the_database_returns(
    database: Forwarder, test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    manager = JobManager(jobs_dir=tmp_path)
    database.down()
    with (
        patch("openscientist.job_container.JobContainerRunner", return_value=MagicMock()),
        patch.object(manager, "_run_job"),
    ):
        recovery = asyncio.create_task(asyncio.to_thread(manager.recover_after_restart))
        await _down_for(database, 2)
        await asyncio.wait_for(recovery, 20)
    assert await _status(test_engine, job_in[JobStatus.RUNNING]) == "cancelled"
    assert str(job_in[JobStatus.QUEUED]) in manager._running_jobs


async def test_a_rejected_recovery_write_keeps_the_rest_of_the_recovery_running(
    database: Forwarder, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A job whose recovery write the database rejects is skipped with a
    logged error, and the recovery goes on with the jobs after it."""
    open_statuses = [
        status.value
        for status in (
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.AWAITING_FEEDBACK,
            JobStatus.GENERATING_REPORT,
        )
    ]
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(delete(Job).where(Job.status.in_(open_statuses)))
        stuck = Job(research_question="Q?", status=JobStatus.PENDING.value)
        other = Job(research_question="Q?", status=JobStatus.PENDING.value)
        session.add_all([stuck, other])
        await session.commit()

    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION os_test_fail_recovery() RETURNS trigger AS $$ "
                "BEGIN IF NEW.id = '"
                f"{stuck.id}' THEN RAISE EXCEPTION 'injected recovery failure'; "
                "END IF; RETURN NEW; END $$ LANGUAGE plpgsql"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER os_test_fail_recovery BEFORE UPDATE ON jobs "
                "FOR EACH ROW EXECUTE FUNCTION os_test_fail_recovery()"
            )
        )
    manager = JobManager(jobs_dir=tmp_path)
    try:
        with patch.object(manager, "_run_job"):
            await asyncio.to_thread(manager.recover_after_restart)
        assert await _status(test_engine, stuck.id) == "pending"
        assert await _status(test_engine, other.id) == "queued"
    finally:
        _stop_manager_background(manager)
        async with test_engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER os_test_fail_recovery ON jobs"))
            await conn.execute(text("DROP FUNCTION os_test_fail_recovery()"))
        async with factory() as session:
            await session.execute(delete(Job).where(Job.id.in_([stuck.id, other.id])))
            await session.commit()


async def test_a_lost_container_is_cancelled_when_cleanup_fails(
    database: Forwarder, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The job is marked cancelled even when the container cleanup fails: the
    cancellation write is not rolled back by the cleanup error."""
    open_statuses = [
        status.value
        for status in (
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.AWAITING_FEEDBACK,
            JobStatus.GENERATING_REPORT,
        )
    ]
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(delete(Job).where(Job.status.in_(open_statuses)))
        running = Job(research_question="Q?", status=JobStatus.RUNNING.value)
        pending = Job(research_question="Q?", status=JobStatus.PENDING.value)
        session.add_all([running, pending])
        await session.commit()

    runner = MagicMock()
    runner.cleanup.side_effect = RuntimeError("docker down")
    manager = JobManager(jobs_dir=tmp_path)
    try:
        with (
            patch.object(manager, "_run_job"),
            patch("openscientist.job_container.JobContainerRunner", return_value=runner),
        ):
            await asyncio.to_thread(manager.recover_after_restart)
        assert await _status(test_engine, running.id) == "cancelled"
        assert await _status(test_engine, pending.id) == "queued"
    finally:
        _stop_manager_background(manager)
        async with factory() as session:
            await session.execute(delete(Job).where(Job.id.in_([running.id, pending.id])))
            await session.commit()


async def test_startup_does_not_wait_for_a_down_database(
    database: Forwarder, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    """Recovery waits out a lost database in the background, so the web
    process starts even when the database is still down, and picks the
    queued job up once it is back."""
    import openscientist.web_app as web_app_module

    manager = JobManager(jobs_dir=tmp_path)
    saved = web_app_module._state.job_manager
    web_app_module._state.job_manager = None
    try:
        with (
            patch.object(manager, "_run_job"),
            patch("openscientist.job_container.JobContainerRunner", return_value=MagicMock()),
            patch("openscientist.web_app._new_job_manager", return_value=manager),
        ):
            try:
                database.down()
                init = asyncio.create_task(
                    asyncio.to_thread(web_app_module._initialize_job_manager_runtime, tmp_path)
                )
                await asyncio.wait_for(init, 1.5)
                assert web_app_module._state.job_manager is manager
                await _down_for(database, 2)
                for _ in range(100):
                    if str(job_in[JobStatus.QUEUED]) in manager._running_jobs:
                        break
                    await asyncio.sleep(0.2)
                assert str(job_in[JobStatus.QUEUED]) in manager._running_jobs
            finally:
                # Stopped while ``_run_job`` is still patched, so nothing the
                # manager starts late can launch a real job.
                for _ in range(100):
                    if not any(
                        t.name == "job-recovery" and t.is_alive() for t in threading.enumerate()
                    ):
                        break
                    await asyncio.sleep(0.2)
                if any(t.name == "job-recovery" for t in threading.enumerate()):
                    pytest.fail("the job-recovery thread is still running at test end")
                _stop_manager_background(manager)
    finally:
        web_app_module._state.job_manager = saved


async def test_a_pick_that_meets_an_outage_starts_the_job_after_it(
    database: Forwarder, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    manager = JobManager(jobs_dir=tmp_path)
    queued = str(job_in[JobStatus.QUEUED])
    with patch.object(manager, "_run_job"):
        try:
            database.down()
            assert not await asyncio.to_thread(manager._start_next_queued_job)
            await _down_for(database, 2)
            for _ in range(100):
                if queued in manager._running_jobs:
                    break
                await asyncio.sleep(0.2)
            assert queued in manager._running_jobs
        finally:
            _stop_manager_background(manager)


async def test_a_pick_before_the_row_is_seeded_starts_the_job_once_it_is(
    test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    """Startup recovery can pick before the web seeds the admin settings row,
    so a pick that finds no row picks again until the row is there."""
    manager = JobManager(jobs_dir=tmp_path)
    queued = str(job_in[JobStatus.QUEUED])
    with patch.object(manager, "_run_job"):
        try:
            async with test_engine.begin() as conn:
                await conn.execute(text("DELETE FROM admin_settings"))
            assert not await asyncio.to_thread(manager._start_next_queued_job)
            await set_admin_settings_row(test_engine, max_concurrent_jobs=5)
            for _ in range(100):
                if queued in manager._running_jobs:
                    break
                await asyncio.sleep(0.2)
            assert queued in manager._running_jobs
        finally:
            _stop_manager_background(manager)


def test_a_pick_error_other_than_an_outage_is_not_scheduled_to_retry(tmp_path: Path) -> None:
    """A pick that fails for a reason the retry cannot fix is logged and
    stopped there: no background repick is scheduled for it."""
    import openscientist.job_manager as job_manager_module

    manager = JobManager(jobs_dir=tmp_path)
    with (
        patch.object(
            job_manager_module, "_db_next_queued_job", side_effect=RuntimeError("pick blew up")
        ),
        patch.object(
            job_manager_module,
            "current_admin_settings_sync",
            return_value=default_admin_settings_values(),
        ),
    ):
        assert not manager._start_next_queued_job()
    assert manager._repick is None
    assert manager._running_jobs == {}


async def test_a_second_outage_reuses_the_pending_repick(
    database: Forwarder, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    """A second pick failure while a repick is already pending does not reset
    the backoff; the job is picked once, after the database returns."""
    manager = JobManager(jobs_dir=tmp_path)
    queued = str(job_in[JobStatus.QUEUED])
    with patch.object(manager, "_run_job"):
        database.down()
        assert not await asyncio.to_thread(manager._start_next_queued_job)
        first = manager._repick
        assert first is not None
        assert not await asyncio.to_thread(manager._start_next_queued_job)
        assert manager._repick is first
        await _down_for(database, 2)
        for _ in range(100):
            if queued in manager._running_jobs:
                break
            await asyncio.sleep(0.2)
        assert queued in manager._running_jobs
    _stop_manager_background(manager)


async def test_a_crashed_container_hands_its_error_to_the_job(
    test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    """The worker sees only the exit code, so the job's error comes from the
    file the container leaves in the job directory, and a stale one is ignored."""
    from openscientist.job.agent_error import write_agent_error

    job = str(job_in[JobStatus.QUEUED])
    (tmp_path / job).mkdir()
    write_agent_error(tmp_path / job, "a previous run's error")
    runner = MagicMock()
    runner.get_exit_code.return_value = 1
    runner.launch.side_effect = lambda job_id, job_dir, **_: write_agent_error(
        job_dir, "DatabaseUnavailableError: Connect call failed"
    )
    manager = JobManager(jobs_dir=tmp_path)
    # A failed job starts the next queued one, which would be another test's.
    with (
        patch("openscientist.job_container.JobContainerRunner", return_value=runner),
        patch("openscientist.job_manager.time.sleep"),
        patch.object(manager, "_start_next_queued_job"),
    ):
        await asyncio.to_thread(manager._run_job_in_container, job)
    async with test_engine.connect() as conn:
        error = await conn.scalar(select(Job.error_message).where(Job.id == UUID(job)))
    assert error == (
        "Agent container exited with code 1: DatabaseUnavailableError: Connect call failed"
    )


async def test_a_poll_error_other_than_an_outage_fails_the_job(
    test_engine: AsyncEngine, job_in: dict[JobStatus, UUID], tmp_path: Path
) -> None:
    """A poll that fails for a reason the retry cannot fix fails the job at
    once; only a lost connection is logged and polled again."""
    job = str(job_in[JobStatus.QUEUED])
    (tmp_path / job).mkdir()
    manager = JobManager(jobs_dir=tmp_path)
    try:
        with (
            patch("openscientist.job_container.JobContainerRunner", return_value=MagicMock()),
            patch.object(manager, "_load_job_info", side_effect=ValueError("poll blew up")),
            patch("openscientist.job_manager.time.sleep"),
            patch.object(manager, "_start_next_queued_job"),
        ):
            await asyncio.to_thread(manager._run_job_in_container, job)
    finally:
        # The worker logs its version, which caches the commit on first read.
        import openscientist.version as version_module

        version_module._commit = None
        version_module._build_time = None
    async with test_engine.connect() as conn:
        row = (
            await conn.execute(select(Job.status, Job.error_message).where(Job.id == UUID(job)))
        ).one()
    assert row.status == "failed"
    assert row.error_message == "poll blew up"

"""Every consumer reads the admin settings row at the moment of use, so an
edit of the row applies at the consumer's next use, without a restart."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openscientist.auth.routes import create_session
from openscientist.container_manager import ContainerManager
from openscientist.database.models import Job, User
from openscientist.exceptions import FileTooBigError
from openscientist.file_loader import validate_uploaded_file
from openscientist.job_container.db_login import AgentLogin
from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_manager import JobManager, JobStatus
from openscientist.providers.base import CostInfo
from openscientist.settings import clear_settings_cache
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

_LOGIN = AgentLogin(
    role="agent_" + "0" * 32,
    database_url="postgresql+asyncpg://agent_x:login-pw@postgres:5432/db",
)


@pytest_asyncio.fixture
async def admin_row(test_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """The admin settings row, which each test seeds and edits."""
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


@pytest.fixture
def no_floor(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """The environment without the airgap floor, so the row alone decides."""
    monkeypatch.setenv("OPENSCIENTIST_AIRGAPPED", "false")
    clear_settings_cache()
    yield
    clear_settings_cache()


def _launcher_settings() -> SimpleNamespace:
    provider = MagicMock()
    provider.get_container_env_vars.return_value = {}
    provider.provider_id = "anthropic"
    return SimpleNamespace(
        container=SimpleNamespace(
            host_project_dir=None,
            container_app_dir="/app",
            agent_network=None,
            agent_platform=None,
            agent_image="openscientist-agent:latest",
        ),
        provider=provider,
        database=SimpleNamespace(
            effective_database_url="postgresql+asyncpg://web:web-pw@postgres:5432/db"
        ),
        phenix=SimpleNamespace(phenix_host_path=None),
        secret_key="master-key",
    )


def _executor_settings() -> SimpleNamespace:
    return SimpleNamespace(
        container=SimpleNamespace(
            container_app_dir="/app",
            host_project_dir=None,
            agent_network="bridge",
            executor_image="openscientist-executor:latest",
        ),
        airgap=SimpleNamespace(enabled=False),
    )


async def _status(test_engine: AsyncEngine, job_id: object) -> str:
    async with test_engine.connect() as conn:
        row = await conn.scalar(select(Job.status).where(Job.id == job_id))
    return str(row)


@pytest.mark.asyncio
async def test_a_launch_reads_the_rows_limits_timeout_and_airgap(
    test_engine: AsyncEngine, admin_row: None, no_floor: None
) -> None:
    """The next launch takes the limits and lifetime from the row, airgap included."""
    from openscientist.job_container.launcher_client import LauncherClient

    await set_admin_settings_row(test_engine)
    runner = JobContainerRunner()
    client = MagicMock(spec=LauncherClient)
    client.create_agent.return_value = {
        "id": "c" * 64,
        "short_id": "abc123",
        "name": "openscientist-agent-job1",
        "status": "running",
    }
    runner._client = client
    _provider = MagicMock()
    _provider.prelaunch_routing_error.return_value = None
    with (
        patch("openscientist.job_container.runner.get_settings", return_value=_launcher_settings()),
        patch("openscientist.job_container.runner.get_provider", return_value=_provider),
        patch("openscientist.job_container.runner.open_agent_login", return_value=_LOGIN) as opened,
        patch(
            "openscientist.agent.factory.agent_class_for_provider_id",
            return_value=MagicMock(RUN_HOME=None),
        ),
    ):
        await asyncio.to_thread(runner.launch, "job-123", Path("/app/jobs/job-123"))
        request = cast(dict[str, object], client.create_agent.call_args.args[0])
        assert request["memory_mib"] == 8192
        assert request["cpu"] == 2.0
        opened.assert_called_once_with("job-123", lifetime_seconds=14400)
        assert "OPENSCIENTIST_AIRGAPPED" not in cast(dict[str, str], request["env"])

        await set_admin_settings_row(
            test_engine,
            agent_cpu=1.5,
            agent_memory_mib=4096,
            agent_timeout_seconds=900,
            airgapped=True,
        )
        await asyncio.to_thread(runner.launch, "job-456", Path("/app/jobs/job-456"))
        request = cast(dict[str, object], client.create_agent.call_args.args[0])
        assert request["memory_mib"] == 4096
        assert request["cpu"] == 1.5
        assert request["airgapped"] is True
        assert cast(dict[str, str], request["env"])["OPENSCIENTIST_AIRGAPPED"] == "1"
        assert opened.call_args.args[0] == "job-456"
        assert opened.call_args.kwargs["lifetime_seconds"] == 900


@pytest.mark.asyncio
async def test_an_execution_reads_the_rows_limits_and_airgap(
    test_engine: AsyncEngine, admin_row: None, no_floor: None, tmp_path: Path
) -> None:
    """The next execution takes the limits, timeout, and airgap flag from the row."""
    await set_admin_settings_row(test_engine)
    manager = ContainerManager()
    client = MagicMock()
    manager._client = client
    client.run_exec.return_value = {
        "stdout": json.dumps(
            {"success": True, "output": "ok", "plots": [], "error": None, "execution_time": 0.1}
        ),
        "exit_code": 0,
    }
    with (
        patch("openscientist.container_manager.get_settings", return_value=_executor_settings()),
    ):
        await asyncio.to_thread(
            manager.execute_code,
            code="print('hi')",
            job_id="exec-1",
            output_dir=str(tmp_path / "out-1"),
        )
        request = cast(dict[str, object], client.run_exec.call_args.args[0])
        assert request["memory_mib"] == 512
        assert request["cpu"] == 0.5
        assert request["airgapped"] is False
        assert cast(dict[str, object], request["input"])["timeout"] == 120

        await set_admin_settings_row(
            test_engine,
            executor_cpu=0.75,
            executor_memory_mib=1024,
            executor_timeout_seconds=60,
            airgapped=True,
        )
        await asyncio.to_thread(
            manager.execute_code,
            code="print('hi')",
            job_id="exec-2",
            output_dir=str(tmp_path / "out-2"),
        )
        request = cast(dict[str, object], client.run_exec.call_args.args[0])
        assert request["memory_mib"] == 1024
        assert request["cpu"] == 0.75
        assert request["airgapped"] is True
        assert cast(dict[str, object], request["input"])["timeout"] == 60


@pytest.mark.asyncio
async def test_a_slot_check_honours_a_lowered_cap(
    test_engine: AsyncEngine, admin_row: None, tmp_path: Path
) -> None:
    """A slot check reads the cap from the row, so lowering it pauses new starts."""
    await set_admin_settings_row(test_engine)  # max_concurrent_jobs = 1
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as session:
        first = Job(research_question="A?", status=JobStatus.PENDING.value)
        second = Job(research_question="B?", status=JobStatus.PENDING.value)
        session.add_all([first, second])
        await session.commit()
    try:
        manager = JobManager(jobs_dir=tmp_path)
        with patch.object(manager, "_run_job"):
            # One slot, one free: the first job starts.
            await asyncio.to_thread(manager.start_job, str(first.id))
            assert str(first.id) in manager._running_jobs

            # A busy slot queues the second job, and a cap of 0 queues it too.
            manager._running_jobs["busy"] = MagicMock()
            await asyncio.to_thread(manager.start_job, str(second.id))
            assert str(second.id) not in manager._running_jobs
            assert await _status(test_engine, second.id) == JobStatus.QUEUED.value

            # A cap of 0 pauses starts even with every slot free.
            manager._running_jobs.clear()
            await set_admin_settings_row(test_engine, max_concurrent_jobs=0)
            await asyncio.to_thread(manager.start_job, str(second.id))
            assert str(second.id) not in manager._running_jobs
            assert await _status(test_engine, second.id) == JobStatus.QUEUED.value
    finally:
        async with factory() as session:
            await session.execute(delete(Job).where(Job.id.in_([first.id, second.id])))
            await session.commit()


@pytest.mark.asyncio
async def test_a_queue_pick_uses_the_rows_window(
    test_engine: AsyncEngine, admin_row: None, tmp_path: Path
) -> None:
    """The pick reads the window from the row, so an edit reorders the next pick."""
    await set_admin_settings_row(test_engine, max_concurrent_jobs=2)
    now = datetime.now(UTC)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(delete(Job).where(Job.status == JobStatus.QUEUED.value))
        alice = User(email=f"alice-{uuid4().hex}@admin-settings.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@admin-settings.test", name="Bob")
        session.add_all([alice, bob])
        await session.flush()

        async def job(owner: User, status: JobStatus, **fields: object) -> Job:
            j = Job(owner_id=owner.id, research_question="Q?", status=status.value)
            session.add(j)
            await session.flush()
            await session.execute(
                text(
                    "UPDATE jobs SET created_at = :created, started_at = :started, "
                    "ended_at = :ended WHERE id = :id"
                ),
                {
                    "created": fields.get("created", now),
                    "started": fields.get("started"),
                    "ended": fields.get("ended"),
                    "id": j.id,
                },
            )
            return j

        # Alice's only hours are old; Bob's one hour is recent.
        await job(
            alice,
            JobStatus.COMPLETED,
            started=now - timedelta(hours=40),
            ended=now - timedelta(hours=35),
        )
        await job(
            bob,
            JobStatus.COMPLETED,
            started=now - timedelta(hours=2),
            ended=now - timedelta(hours=1),
        )
        alices_queued = await job(alice, JobStatus.QUEUED, created=now - timedelta(hours=2))
        bobs_queued = await job(bob, JobStatus.QUEUED, created=now - timedelta(minutes=5))
        await session.commit()
    try:
        # With a 24 h window only Bob's recent hour counts, so Alice goes first.
        await set_admin_settings_row(
            test_engine, max_concurrent_jobs=2, scheduler_window_hours=24.0
        )
        manager = JobManager(jobs_dir=tmp_path)
        with patch.object(manager, "_run_job"):
            await asyncio.to_thread(manager._start_next_queued_job)
            assert set(manager._running_jobs) == {str(alices_queued.id)}

            # Widening the window to 48 h counts Alice's old hours, so Bob goes next.
            await set_admin_settings_row(
                test_engine, max_concurrent_jobs=2, scheduler_window_hours=48.0
            )
            await asyncio.to_thread(manager._start_next_queued_job)
            assert str(bobs_queued.id) in manager._running_jobs
    finally:
        async with factory() as session:
            await session.execute(delete(Job).where(Job.owner_id.in_([alice.id, bob.id])))
            await session.execute(delete(User).where(User.id.in_([alice.id, bob.id])))
            await session.commit()


@pytest.mark.asyncio
async def test_an_upload_honours_a_lowered_size_limit(
    test_engine: AsyncEngine, admin_row: None, tmp_path: Path
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    path = tmp_path / "big.csv"
    path.write_text("a,b\n" + "1,2\n" * 300_000)
    with pytest.raises(FileTooBigError):
        await asyncio.to_thread(validate_uploaded_file, path)
    await set_admin_settings_row(test_engine, max_file_size_mb=1000)
    await asyncio.to_thread(validate_uploaded_file, path)


@pytest.mark.asyncio
async def test_a_login_session_lasting_the_rows_days(
    test_engine: AsyncEngine, admin_row: None, db_session: AsyncSession, test_user: User
) -> None:
    await set_admin_settings_row(test_engine, session_duration_days=30)
    first = await create_session(db_session, str(test_user.id))
    assert abs((first.expires_at - datetime.now(UTC)) - timedelta(days=30)) < timedelta(minutes=1)

    await set_admin_settings_row(test_engine, session_duration_days=7)
    second = await create_session(db_session, str(test_user.id))
    assert abs((second.expires_at - datetime.now(UTC)) - timedelta(days=7)) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_a_budget_check_honours_the_rows_limits(
    test_engine: AsyncEngine, admin_row: None
) -> None:
    """A NULL limit means no limit, and a lowered cap refuses the next check."""
    from openscientist.budget import check_budget, warning_limit_reached

    cost = CostInfo(
        provider_name="stub",
        total_spend_usd=600.0,
        recent_spend_usd=95.0,
        recent_period_hours=24,
    )
    provider = SimpleNamespace(id="stub", get_cost_info=lambda lookback_hours=24: cost)

    with patch("openscientist.budget.get_provider", return_value=provider):
        await set_admin_settings_row(
            test_engine,
            max_project_spend_total_usd=500.0,
            max_project_spend_24h_usd=100.0,
            warn_project_spend_24h_usd=90.0,
        )
        verdict = await check_budget(provider_id="stub")
        assert verdict.ok is False
        assert verdict.limit == "project_total"

        # The 24 h figure is under its hard limit but at the warning.
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=None)
        verdict = await check_budget(provider_id="stub")
        assert verdict.ok is True
        assert warning_limit_reached(95.0, 90.0, 100.0) is True

        await set_admin_settings_row(test_engine, max_project_spend_24h_usd=90.0)
        verdict = await check_budget(provider_id="stub")
        assert verdict.ok is False
        assert verdict.limit == "project_24h"

        await set_admin_settings_row(
            test_engine, max_project_spend_total_usd=None, max_project_spend_24h_usd=None
        )
        verdict = await check_budget(provider_id="stub")
        assert verdict.ok is True
        assert verdict.limit is None

"""Tests for job_manager module."""

import asyncio
import threading
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from openscientist.database.models import Job
from openscientist.job_manager import JobInfo, JobManager, JobStatus, JobStatusUpdateResult
from openscientist.settings import ProviderSettings


class TestJobStatus:
    """Tests for the JobStatus enum."""

    def test_all_statuses_exist(self):
        expected = {
            "pending",
            "queued",
            "running",
            "awaiting_feedback",
            "generating_report",
            "completed",
            "failed",
            "cancelled",
        }
        actual = {s.value for s in JobStatus}
        assert actual == expected

    def test_string_enum(self):
        assert JobStatus.RUNNING == "running"
        assert isinstance(JobStatus.RUNNING, str)


class TestJobInfo:
    """Tests for JobInfo data handling."""

    def test_to_dict(self):
        info = JobInfo(
            job_id="j1",
            research_question="Q?",
            status=JobStatus.COMPLETED,
            created_at="2026-01-01T00:00:00",
            max_iterations=10,
        )
        d = info.to_dict()
        assert d["status"] == "completed"
        assert d["job_id"] == "j1"

    def test_from_db_model_maps_llm_metadata(self):
        job = _make_db_job(
            "completed",
            "2026-01-01T00:00:00",
            llm_provider="anthropic",
            llm_config={"model": "claude-sonnet-4-5-20250929"},
        )

        info = JobInfo.from_db_model(job)

        assert info.llm_provider == "anthropic"
        assert info.llm_model == "claude-sonnet-4-5-20250929"


def _write_config(jobs_dir: Path, job_id: str, **overrides) -> dict:
    """Helper: create a minimal job directory scaffold."""
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return {
        "job_id": job_id,
        "research_question": "Test?",
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
        "max_iterations": 10,
        "investigation_mode": "autonomous",
        **overrides,
    }


def _make_db_job(
    status: str,
    created_at: str,
    job_id: str | None = None,
    **overrides,
) -> SimpleNamespace:
    """Helper: create a fake DB job model."""
    job_uuid = UUID(job_id) if job_id else uuid4()
    created_dt = datetime.fromisoformat(created_at).replace(tzinfo=UTC)
    defaults = {
        "id": job_uuid,
        "research_question": "Test?",
        "status": status,
        "created_at": created_dt,
        "updated_at": created_dt,
        "max_iterations": 10,
        "current_iteration": 0,
        "error_message": None,
        "cancellation_reason": None,
        "investigation_mode": "autonomous",
        "owner_id": None,
        "short_title": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _db_get_job_side_effect(models: list[SimpleNamespace]):
    """Build side-effect that returns models by UUID id."""
    by_id = {m.id: m for m in models}

    async def _inner(job_id: str, user_id=None):
        _ = user_id
        try:
            return by_id.get(UUID(job_id))
        except ValueError:
            return None

    return _inner


def _new_manager(tmp_path: Path) -> JobManager:
    """Construct a manager without real DB access during init."""
    with patch("openscientist.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=[]):
        return JobManager(jobs_dir=tmp_path)


class TestJobManagerInit:
    """Tests for JobManager initialization."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    def test_creates_jobs_dir(self, tmp_path):
        d = tmp_path / "myjobs"
        assert not d.exists()
        _new_manager(d)
        assert d.exists()


class TestJobManagerListAndGet:
    """Tests for listing and getting jobs."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress",
            new_callable=AsyncMock,
            return_value=(0, 0),
        ):
            yield

    @pytest.fixture
    def manager(self, tmp_path) -> JobManager:
        return _new_manager(tmp_path)

    async def test_get_existing_job(self, manager):
        db_job = _make_db_job("completed", "2026-02-01T00:00:00")
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
        ):
            job = await manager.get_job(str(db_job.id))
        assert job is not None
        assert job.status == JobStatus.COMPLETED

    async def test_get_nonexistent_returns_none(self, manager):
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=None
        ):
            assert await manager.get_job(str(uuid4())) is None


class TestJobManagerRegenerateReport:
    """Tests for the admin report-regeneration path."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress",
            new_callable=AsyncMock,
            return_value=(0, 0),
        ):
            yield

    @pytest.fixture
    def manager(self, tmp_path) -> JobManager:
        return _new_manager(tmp_path)

    async def test_missing_job_raises(self, manager):
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=None
        ):
            with pytest.raises(ValueError, match="not found"):
                await manager.regenerate_report(str(uuid4()))

    async def test_non_completed_raises(self, manager):
        db_job = _make_db_job("running", "2026-02-01T00:00:00")
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
        ):
            with pytest.raises(ValueError, match="not completed"):
                await manager.regenerate_report(str(db_job.id))

    async def test_already_running_raises(self, manager):
        db_job = _make_db_job("completed", "2026-02-01T00:00:00")
        manager._running_jobs[str(db_job.id)] = MagicMock()
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
        ):
            with pytest.raises(ValueError, match="already running"):
                await manager.regenerate_report(str(db_job.id))

    async def test_launches_in_report_only_mode(self, manager):
        """A completed job spawns a worker thread that runs the container in
        report-only mode and is tracked as running."""
        db_job = _make_db_job("completed", "2026-02-01T00:00:00")
        job_id = str(db_job.id)
        fake_thread = MagicMock()

        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                return_value=db_job,
            ),
            patch(
                "openscientist.job_manager.threading.Thread", return_value=fake_thread
            ) as mock_thread,
        ):
            await manager.regenerate_report(job_id)

        assert manager._running_jobs[job_id] is fake_thread
        fake_thread.start.assert_called_once()
        # The worker targets _run_job in report-only mode (no discovery loop).
        kwargs = mock_thread.call_args.kwargs
        assert kwargs["target"] == manager._run_job
        assert kwargs["args"] == (job_id,)
        assert kwargs["kwargs"] == {"run_mode": "report_only"}


class TestJobManagerDelete:
    """Tests for job deletion."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress",
            new_callable=AsyncMock,
            return_value=(0, 0),
        ):
            yield

    async def test_delete_completed_job(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="completed")
        db_job = _make_db_job("completed", "2026-02-01T00:00:00", job_id=job_id)

        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                side_effect=_db_get_job_side_effect([db_job]),
            ),
            patch("openscientist.job_manager._db_delete_job", new_callable=AsyncMock),
        ):
            await manager.delete_job(job_id)

        assert not (tmp_path / job_id).exists()

    async def test_delete_nonexistent_raises(self, tmp_path):
        manager = _new_manager(tmp_path)
        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            await manager.delete_job(str(uuid4()))

    async def test_delete_running_raises(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        db_job = _make_db_job("running", "2026-02-01T00:00:00", job_id=job_id)

        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                side_effect=_db_get_job_side_effect([db_job]),
            ),
            pytest.raises(ValueError, match="Cannot delete running"),
        ):
            await manager.delete_job(job_id)

    async def test_delete_aborts_when_database_delete_fails(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="completed")
        db_job = _make_db_job("completed", "2026-02-01T00:00:00", job_id=job_id)

        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                side_effect=_db_get_job_side_effect([db_job]),
            ),
            patch(
                "openscientist.job_manager._db_delete_job",
                new_callable=AsyncMock,
                side_effect=RuntimeError("database unavailable"),
            ),
            pytest.raises(ValueError, match=f"Failed to delete job {job_id} from database"),
        ):
            await manager.delete_job(job_id)

        assert (tmp_path / job_id).exists()


class TestContainerStatusNotifications:
    """The web poll of a running container sends the notifications for the
    transitions the container writes, so the container never reads users."""

    def _run(
        self,
        tmp_path: Path,
        statuses: list[tuple[JobStatus, str | None]],
    ) -> list[Mapping[str, object]]:
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        owner_id = uuid4()
        polls = [
            JobInfo(
                job_id=job_id,
                research_question="Q?",
                status=status,
                created_at="2026-02-01T00:00:00+00:00",
                error=error,
            )
            for status, error in statuses
        ]
        target = JobStatusUpdateResult(
            applied=True,
            ntfy_enabled=True,
            ntfy_topic="topic-1",
            owner_id=str(owner_id),
            job_title="Boiling water",
            current_iteration=3,
        )
        runner = MagicMock()
        runner.get_exit_code.return_value = None
        notify = AsyncMock(return_value=True)
        with (
            patch("openscientist.job_container.JobContainerRunner", return_value=runner),
            patch("openscientist.job_manager.time.sleep"),
            patch(
                "openscientist.settings.get_settings",
                return_value=SimpleNamespace(container=SimpleNamespace(agent_timeout=3600)),
            ),
            patch.object(manager, "_load_job_info", side_effect=polls),
            patch.object(manager, "_start_next_queued_job"),
            patch(
                "openscientist.job_manager._db_update_job_status",
                new_callable=AsyncMock,
                return_value=target,
            ),
            patch(
                "openscientist.job_manager._db_get_notification_target",
                new_callable=AsyncMock,
                return_value=target,
            ),
            patch("openscientist.job_manager.notify_job_status_change", notify),
        ):
            manager._run_job_in_container(job_id)
        return [call.kwargs for call in notify.await_args_list]

    def test_feedback_wait_and_completion_notify_once_each(self, tmp_path):
        sent = self._run(
            tmp_path,
            [
                (JobStatus.RUNNING, None),
                (JobStatus.AWAITING_FEEDBACK, None),
                (JobStatus.AWAITING_FEEDBACK, None),
                (JobStatus.RUNNING, None),
                (JobStatus.COMPLETED, None),
            ],
        )
        assert [s["new_status"] for s in sent] == ["running", "awaiting_feedback", "completed"]
        # Iteration 3 is already current while the loop waits after iteration 2.
        assert sent[1]["iteration"] == 2
        assert sent[1]["ntfy_topic"] == "topic-1"

    def test_a_failure_written_by_the_container_notifies_with_its_error(self, tmp_path):
        sent = self._run(tmp_path, [(JobStatus.FAILED, "Report generation failed: boom")])
        assert [s["new_status"] for s in sent] == ["running", "failed"]
        assert sent[1]["error_message"] == "Report generation failed: boom"

    def test_a_cancellation_is_not_notified_twice(self, tmp_path):
        """cancel_job already notified when it wrote the status."""
        sent = self._run(tmp_path, [(JobStatus.CANCELLED, None)])
        assert [s["new_status"] for s in sent] == ["running"]


class TestJobManagerKSProgress:
    """Tests for real-time KS progress reading."""

    async def test_running_job_reads_ks(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="running")
        ks_data = {
            "iteration": 5,
            "findings": [{"id": "F001"}, {"id": "F002"}],
        }
        db_job = _make_db_job("running", "2026-02-01T00:00:00", job_id=job_id)

        with (
            patch(
                "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
            ),
            patch(
                "openscientist.job_manager.thread_safe_admin_session", return_value=nullcontext()
            ),
            patch(
                "openscientist.job_manager.KnowledgeState.load_from_database",
                new_callable=AsyncMock,
                return_value=MagicMock(data=ks_data),
            ),
        ):
            job = await manager.get_job(job_id)

        assert job is not None
        assert job.iterations_completed == 4
        assert job.findings_count == 2


class TestJobManagerCancellationConcurrency:
    """Tests for cancellation behavior and active-slot accounting."""

    async def test_cancel_running_job_keeps_thread_registered_until_exit(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()
        db_job = _make_db_job("running", "2026-02-01T00:00:00", job_id=job_id)

        with (
            patch(
                "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
            ),
            patch(
                "openscientist.job_manager._db_update_job_status",
                new_callable=AsyncMock,
                return_value=JobStatusUpdateResult(applied=True),
            ),
            patch.object(manager, "_stop_container"),
            patch.object(manager, "_start_next_queued_job"),
        ):
            await manager.cancel_job(job_id)

        # Cancellation is cooperative; keep tracking until worker thread exits.
        assert job_id in manager._running_jobs

    def test_active_job_count_keeps_cancelled_threads_until_exit(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()

        assert manager._get_active_job_count() == 1


class TestJobManagerConcurrencyLimit:
    """Slot accounting against max_concurrent, on real database rows."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    async def test_job_awaiting_feedback_holds_its_slot(
        self, tmp_path: Path, test_engine: AsyncEngine
    ) -> None:
        """A paused coinvestigate job keeps its container, so a new job must queue."""
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        paused = Job(
            research_question="Paused?",
            status="awaiting_feedback",
            investigation_mode="coinvestigate",
        )
        new = Job(research_question="New?", status="pending")
        async with session_factory() as session:
            session.add_all([paused, new])
            await session.commit()

        try:
            manager = _new_manager(tmp_path)
            manager.max_concurrent = 1
            manager._running_jobs[str(paused.id)] = MagicMock()

            with patch.object(manager, "_run_job"):
                await asyncio.to_thread(manager.start_job, str(new.id))

            assert str(new.id) not in manager._running_jobs
            async with session_factory() as session:
                stored = await session.get(Job, new.id)
                assert stored is not None
                assert stored.status == "queued"
        finally:
            async with session_factory() as session:
                await session.execute(delete(Job).where(Job.id.in_([paused.id, new.id])))
                await session.commit()


class TestJobManagerCleanup:
    """Tests for old job cleanup."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    def test_cleanup_old_jobs(self, tmp_path):
        manager = _new_manager(tmp_path)

        old_id = str(uuid4())
        new_id = str(uuid4())
        _write_config(tmp_path, old_id, status="failed")
        _write_config(tmp_path, new_id, status="failed")

        old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        new_date = datetime.now(UTC).isoformat()

        old_job = _make_db_job("failed", old_date, job_id=old_id)
        new_job = _make_db_job("failed", new_date, job_id=new_id)

        with (
            patch(
                "openscientist.job_manager._db_list_jobs",
                new_callable=AsyncMock,
                return_value=[old_job, new_job],
            ),
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                side_effect=_db_get_job_side_effect([old_job, new_job]),
            ),
            patch("openscientist.job_manager._db_delete_job", new_callable=AsyncMock),
        ):
            deleted = manager.cleanup_old_jobs(days=7, keep_completed=True)

        assert deleted == 1
        assert not (tmp_path / old_id).exists()
        assert (tmp_path / new_id).exists()

    def test_cleanup_keeps_completed(self, tmp_path):
        manager = _new_manager(tmp_path)

        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="completed")
        old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        old_completed = _make_db_job("completed", old_date, job_id=job_id)

        with (
            patch(
                "openscientist.job_manager._db_list_jobs",
                new_callable=AsyncMock,
                return_value=[old_completed],
            ),
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                side_effect=_db_get_job_side_effect([old_completed]),
            ),
            patch("openscientist.job_manager._db_delete_job", new_callable=AsyncMock),
        ):
            deleted = manager.cleanup_old_jobs(days=7, keep_completed=True)

        assert deleted == 0
        assert (tmp_path / job_id).exists()


class TestJobManagerCreationSafety:
    """Tests for creation-time safety guards."""

    async def test_create_job_blocks_when_budget_limit_exceeded(self, tmp_path):
        manager = _new_manager(tmp_path)
        fake_provider = SimpleNamespace(
            check_budget_limits=lambda: {
                "can_proceed": False,
                "errors": ["Provider budget exhausted"],
            }
        )

        with (
            patch.object(manager, "get_job", new_callable=AsyncMock, return_value=None),
            patch("openscientist.job_manager.get_provider", return_value=fake_provider),
            pytest.raises(ValueError, match="Cannot create job"),
        ):
            await manager.create_job(
                job_id=str(uuid4()),
                research_question="Budget test",
                data_files=[],
                auto_start=False,
            )

    async def test_create_job_rolls_back_database_when_filesystem_init_fails(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        fake_provider = SimpleNamespace(check_budget_limits=lambda: {"can_proceed": True})

        with (
            patch.object(manager, "get_job", new_callable=AsyncMock, return_value=None),
            patch("openscientist.job_manager.get_provider", return_value=fake_provider),
            patch("openscientist.job_manager._db_create_job", new_callable=AsyncMock),
            patch(
                "openscientist.job_manager._db_delete_job", new_callable=AsyncMock
            ) as mock_db_delete,
            patch("openscientist.job_manager.create_job", side_effect=RuntimeError("disk full")),
            pytest.raises(ValueError, match="Failed to initialize job files"),
        ):
            await manager.create_job(
                job_id=job_id,
                research_question="Filesystem failure test",
                data_files=[],
                auto_start=False,
            )

        assert mock_db_delete.await_count == 1

    async def test_create_job_records_llm_provider_and_model(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        created_job = JobInfo(
            job_id=job_id,
            research_question="Model tracking test",
            status=JobStatus.PENDING,
            created_at="2026-01-01T00:00:00",
        )
        settings = SimpleNamespace(
            provider=SimpleNamespace(
                provider_id="Anthropic",
                model=None,
                anthropic_default_sonnet_model="claude-sonnet-4-5",
            )
        )

        with (
            patch.object(
                manager, "get_job", new_callable=AsyncMock, side_effect=[None, created_job]
            ),
            patch.object(manager, "_check_budget_before_creation"),
            patch.object(manager, "_create_job_files"),
            patch("openscientist.settings.get_settings", return_value=settings),
            patch.object(
                manager, "_create_db_job_record", new_callable=AsyncMock
            ) as mock_create_db_job_record,
        ):
            await manager.create_job(
                job_id=job_id,
                research_question="Model tracking test",
                data_files=[],
                auto_start=False,
            )

        _, kwargs = mock_create_db_job_record.call_args
        assert kwargs["llm_provider"] == "anthropic"
        assert kwargs["llm_config"] == {"model": "claude-sonnet-4-5"}

    async def test_create_job_with_auto_start_launches_the_job_off_the_loop(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        created_job = JobInfo(
            job_id=job_id,
            research_question="Start me",
            status=JobStatus.PENDING,
            created_at="2026-01-01T00:00:00",
        )
        settings = SimpleNamespace(
            provider=SimpleNamespace(
                provider_id="Anthropic",
                model=None,
                anthropic_default_sonnet_model="claude-sonnet-4-5",
            )
        )
        starts: list[tuple[str, int]] = []

        def record_start(jid: str) -> None:
            starts.append((jid, threading.get_ident()))

        loop_thread = threading.get_ident()

        with (
            patch.object(
                manager, "get_job", new_callable=AsyncMock, side_effect=[None, created_job]
            ),
            patch.object(manager, "_check_budget_before_creation"),
            patch.object(manager, "_create_job_files"),
            patch("openscientist.settings.get_settings", return_value=settings),
            patch.object(manager, "_create_db_job_record", new_callable=AsyncMock),
            patch.object(manager, "start_job", side_effect=record_start),
        ):
            info = await manager.create_job(
                job_id=job_id,
                research_question="Start me",
                data_files=[],
                auto_start=True,
            )

        assert info is created_job
        # An auto-started job is started, and its DB-bound launch runs off the loop thread.
        assert starts == [(job_id, starts[0][1])]
        assert starts[0][1] != loop_thread


class TestJobManagerSummary:
    """Tests for the job summary."""

    async def test_get_job_summary_counts_statuses_and_pulls_cost_off_the_loop(self, tmp_path):
        manager = _new_manager(tmp_path)
        db_jobs = [
            _make_db_job("completed", "2026-02-01T00:00:00"),
            _make_db_job("running", "2026-02-02T00:00:00"),
        ]
        loop_thread = threading.get_ident()
        cost_threads: list[int] = []
        seen_lookbacks: list[int] = []

        def fake_get_cost_info(lookback_hours: int = 24):
            seen_lookbacks.append(lookback_hours)
            cost_threads.append(threading.get_ident())
            return SimpleNamespace(total_spend_usd=12.5)

        fake_provider = SimpleNamespace(
            get_cost_info=fake_get_cost_info,
            evaluate_budget=lambda info: {"errors": [], "warnings": []},
        )

        with (
            patch(
                "openscientist.job_manager._db_list_jobs",
                new_callable=AsyncMock,
                return_value=db_jobs,
            ),
            patch(
                "openscientist.job_manager._load_progress",
                new_callable=AsyncMock,
                return_value=(0, 0),
            ),
            patch("openscientist.job_manager.get_provider", return_value=fake_provider),
        ):
            summary = await manager.get_job_summary()

        assert summary["total_jobs"] == 2
        assert summary["status_counts"]["completed"] == 1
        assert summary["status_counts"]["running"] == 1
        assert summary["cost_info"].total_spend_usd == 12.5
        assert summary["budget_check"] == {"errors": [], "warnings": []}
        # The provider's blocking cost call runs on a worker thread, with the 24h window.
        assert seen_lookbacks == [24]
        assert cost_threads and cost_threads[0] != loop_thread


class TestJobManagerCLI:
    """Tests for job_manager CLI argument handling."""

    def test_list_command_prints_the_job_table(self, monkeypatch, capsys):
        from openscientist.job_manager import main

        job_info = JobInfo(
            job_id="j-1234",
            research_question="Q?",
            status=JobStatus.RUNNING,
            created_at="2026-01-01T00:00:00",
        )
        fake = MagicMock()
        fake.list_jobs = AsyncMock(return_value=[job_info])
        monkeypatch.setattr("openscientist.job_manager.JobManager", MagicMock(return_value=fake))
        monkeypatch.setattr("sys.argv", ["job_manager.py", "list"])

        main()

        fake.list_jobs.assert_awaited_once_with(status=None, limit=None)
        stdout = capsys.readouterr().out
        assert "j-1234" in stdout
        assert "running" in stdout

    def test_get_command_prints_the_job_json(self, monkeypatch, capsys):
        from openscientist.job_manager import main

        job_id = str(uuid4())
        job_info = JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=JobStatus.COMPLETED,
            created_at="2026-01-01T00:00:00",
        )
        fake = MagicMock()
        fake.get_job = AsyncMock(return_value=job_info)
        monkeypatch.setattr("openscientist.job_manager.JobManager", MagicMock(return_value=fake))
        monkeypatch.setattr("sys.argv", ["job_manager.py", "get", job_id])

        main()

        fake.get_job.assert_awaited_once_with(job_id)
        assert f'"job_id": "{job_id}"' in capsys.readouterr().out

    def test_delete_command_deletes_the_job(self, monkeypatch, capsys):
        from openscientist.job_manager import main

        job_id = str(uuid4())
        fake = MagicMock()
        fake.delete_job = AsyncMock()
        monkeypatch.setattr("openscientist.job_manager.JobManager", MagicMock(return_value=fake))
        monkeypatch.setattr("sys.argv", ["job_manager.py", "delete", job_id])

        main()

        fake.delete_job.assert_awaited_once_with(job_id)
        assert f"Deleted job {job_id}" in capsys.readouterr().out

    def test_summary_command_prints_the_counts(self, monkeypatch, capsys):
        from openscientist.job_manager import main

        summary = {
            "total_jobs": 3,
            "status_counts": {"running": 1},
            "cost_info": None,
            "budget_check": None,
        }
        fake = MagicMock()
        fake.get_job_summary = AsyncMock(return_value=summary)
        monkeypatch.setattr("openscientist.job_manager.JobManager", MagicMock(return_value=fake))
        monkeypatch.setattr("sys.argv", ["job_manager.py", "summary"])

        main()

        assert '"total_jobs": 3' in capsys.readouterr().out


class TestEffectiveModel:
    """`_effective_model` records the model the active provider will actually
    use, so the job can show a model badge."""

    @staticmethod
    def _settings(
        model: str | None = None, anthropic_default: str | None = None
    ) -> ProviderSettings:
        return ProviderSettings.model_construct(
            model=model, anthropic_default_sonnet_model=anthropic_default
        )

    def test_explicit_model_wins(self) -> None:
        from openscientist.job_manager import _effective_model

        assert _effective_model(self._settings(model="gpt-5")) == "gpt-5"

    def test_anthropic_default_used(self) -> None:
        from openscientist.job_manager import _effective_model

        assert _effective_model(self._settings(anthropic_default="claude-x")) == "claude-x"

    def test_codex_provider_model_resolved_when_unset(self) -> None:
        """Codex providers carry the model in provider config or a provider default,
        so it is resolved from the provider rather than OPENSCIENTIST_MODEL."""
        from openscientist.job_manager import _effective_model
        from tests.helpers import StubCodexProvider

        with patch("openscientist.job_manager.get_provider", return_value=StubCodexProvider()):
            assert _effective_model(self._settings()) == "stub-codex-model"

    def test_claude_provider_model_resolved_when_unset(self) -> None:
        from openscientist.job_manager import _effective_model
        from tests.helpers import StubClaudeProvider

        with patch("openscientist.job_manager.get_provider", return_value=StubClaudeProvider()):
            assert _effective_model(self._settings()) is not None

    def test_returns_none_when_provider_unavailable(self) -> None:
        from openscientist.job_manager import _effective_model

        with patch(
            "openscientist.job_manager.get_provider", side_effect=ValueError("misconfigured")
        ):
            assert _effective_model(self._settings()) is None

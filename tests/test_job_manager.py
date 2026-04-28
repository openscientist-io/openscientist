"""Tests for job_manager module."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from openscientist.job_manager import JobInfo, JobManager, JobStatus, JobStatusUpdateResult


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

    def test_from_dict(self):
        d = {
            "job_id": "j1",
            "research_question": "Q?",
            "status": "running",
            "created_at": "2026-01-01T00:00:00",
            "max_iterations": 10,
            "iterations_completed": 3,
            "findings_count": 1,
        }
        info = JobInfo.from_dict(d)
        assert info.status == JobStatus.RUNNING
        assert info.iterations_completed == 3

    def test_from_dict_roundtrip(self):
        original = JobInfo(
            job_id="j1",
            research_question="Q?",
            status=JobStatus.FAILED,
            created_at="2026-01-01T00:00:00",
            error="Something broke",
        )
        restored = JobInfo.from_dict(original.to_dict())
        assert restored.job_id == original.job_id
        assert restored.status == original.status
        assert restored.error == original.error

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
        "use_skills": True,
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
        "use_skills": True,
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

    def test_cleans_stale_running_jobs(self, tmp_path):
        stale1 = str(uuid4())
        stale2 = str(uuid4())
        ok = str(uuid4())

        _write_config(tmp_path, stale1, status="running")
        _write_config(tmp_path, stale2, status="queued")
        _write_config(tmp_path, ok, status="completed")

        db_jobs = [
            _make_db_job("running", "2026-02-01T00:00:00", job_id=stale1),
            _make_db_job("queued", "2026-02-01T00:00:00", job_id=stale2),
            _make_db_job("completed", "2026-02-01T00:00:00", job_id=ok),
        ]

        with (
            patch(
                "openscientist.job_manager._db_list_jobs",
                new_callable=AsyncMock,
                return_value=db_jobs,
            ),
            patch(
                "openscientist.job_manager._db_update_job_status",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            JobManager(jobs_dir=tmp_path)

        assert mock_update.await_count == 2


class TestJobManagerListAndGet:
    """Tests for listing and getting jobs."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    @pytest.fixture
    def db_jobs(self):
        return [
            _make_db_job("completed", "2026-02-03T00:00:00"),
            _make_db_job("failed", "2026-02-02T00:00:00"),
            _make_db_job("completed", "2026-02-01T00:00:00"),
        ]

    @pytest.fixture
    def manager(self, tmp_path) -> JobManager:
        return _new_manager(tmp_path)

    def test_list_all_jobs(self, manager, db_jobs):
        with patch(
            "openscientist.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=db_jobs
        ):
            jobs = manager.list_jobs()
        assert len(jobs) == 3

    def test_list_sorted_newest_first(self, manager, db_jobs):
        with patch(
            "openscientist.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=db_jobs
        ):
            jobs = manager.list_jobs()
        assert jobs[0].created_at > jobs[-1].created_at

    def test_list_passes_status_filter(self, manager, db_jobs):
        with patch(
            "openscientist.job_manager._db_list_jobs",
            new_callable=AsyncMock,
            return_value=[db_jobs[1]],
        ) as mock:
            jobs = manager.list_jobs(status=JobStatus.FAILED)
            mock.assert_called_once_with(status=JobStatus.FAILED, limit=None)
        assert len(jobs) == 1

    def test_list_passes_limit(self, manager, db_jobs):
        with patch(
            "openscientist.job_manager._db_list_jobs",
            new_callable=AsyncMock,
            return_value=db_jobs[:2],
        ) as mock:
            jobs = manager.list_jobs(limit=2)
            mock.assert_called_once_with(status=None, limit=2)
        assert len(jobs) == 2

    def test_get_existing_job(self, manager):
        db_job = _make_db_job("completed", "2026-02-01T00:00:00")
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
        ):
            job = manager.get_job(str(db_job.id))
        assert job is not None
        assert job.status == JobStatus.COMPLETED

    def test_get_nonexistent_returns_none(self, manager):
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=None
        ):
            assert manager.get_job(str(uuid4())) is None


class TestJobManagerDelete:
    """Tests for job deletion."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    def test_delete_completed_job(self, tmp_path):
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
            manager.delete_job(job_id)

        assert not (tmp_path / job_id).exists()

    def test_delete_nonexistent_raises(self, tmp_path):
        manager = _new_manager(tmp_path)
        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            manager.delete_job(str(uuid4()))

    def test_delete_running_raises(self, tmp_path):
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
            manager.delete_job(job_id)

    def test_delete_aborts_when_database_delete_fails(self, tmp_path):
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
            manager.delete_job(job_id)

        assert (tmp_path / job_id).exists()


class TestJobManagerStatusUpdate:
    """Tests for status update logic."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    def test_update_status_calls_database(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="pending")

        with patch(
            "openscientist.job_manager._db_update_job_status",
            new_callable=AsyncMock,
            return_value=JobStatusUpdateResult(),
        ) as mock_update:
            manager._update_job_status(job_id, JobStatus.RUNNING)

        mock_update.assert_awaited_once()
        assert mock_update.await_args.args[0] == job_id
        assert mock_update.await_args.args[1] == JobStatus.RUNNING

    def test_completed_passes_status_to_database(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="pending")

        with patch(
            "openscientist.job_manager._db_update_job_status",
            new_callable=AsyncMock,
            return_value=JobStatusUpdateResult(),
        ) as mock_update:
            manager._update_job_status(job_id, JobStatus.COMPLETED)

        assert mock_update.await_args.args[1] == JobStatus.COMPLETED

    def test_failed_passes_status_to_database(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="pending")

        with patch(
            "openscientist.job_manager._db_update_job_status",
            new_callable=AsyncMock,
            return_value=JobStatusUpdateResult(),
        ) as mock_update:
            manager._update_job_status(job_id, JobStatus.FAILED)

        assert mock_update.await_args.args[1] == JobStatus.FAILED


class TestJobManagerKSProgress:
    """Tests for real-time KS progress reading."""

    def test_running_job_reads_ks(self, tmp_path):
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
                "openscientist.job_manager.KnowledgeState.load_from_database_sync",
                return_value=MagicMock(data=ks_data),
            ),
        ):
            job = manager.get_job(job_id)

        assert job is not None
        assert job.iterations_completed == 4
        assert job.findings_count == 2


class TestJobManagerCoinvestigate:
    """Tests for co-investigate mode helpers."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    def test_start_next_queued_job_uses_db_jobs(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, status="queued")
        queued_db_job = _make_db_job("queued", "2026-02-01T00:00:00", job_id=job_id)
        fake_thread = MagicMock()

        with (
            patch(
                "openscientist.job_manager._db_list_jobs",
                new_callable=AsyncMock,
                return_value=[queued_db_job],
            ),
            patch("openscientist.job_manager.threading.Thread", return_value=fake_thread),
        ):
            manager._start_next_queued_job()

        assert manager._running_jobs[job_id] is fake_thread
        fake_thread.start.assert_called_once()

    def test_coinvestigate_count(self, tmp_path):
        manager = _new_manager(tmp_path)

        c1 = str(uuid4())
        c2 = str(uuid4())
        a1 = str(uuid4())

        _write_config(tmp_path, c1, investigation_mode="coinvestigate")
        _write_config(tmp_path, c2, investigation_mode="coinvestigate")
        _write_config(tmp_path, a1, investigation_mode="autonomous")

        db_jobs = [
            _make_db_job(
                "completed",
                "2026-02-01T00:00:00",
                job_id=c1,
                investigation_mode="coinvestigate",
            ),
            _make_db_job(
                "awaiting_feedback",
                "2026-02-01T00:00:00",
                job_id=c2,
                investigation_mode="coinvestigate",
            ),
            _make_db_job(
                "running",
                "2026-02-01T00:00:00",
                job_id=a1,
                investigation_mode="autonomous",
            ),
        ]

        with patch(
            "openscientist.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=db_jobs
        ):
            assert manager.get_coinvestigate_count() == 1

    def test_can_start_coinvestigate_under_limit(self, tmp_path):
        manager = _new_manager(tmp_path)
        with patch(
            "openscientist.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=[]
        ):
            assert manager.can_start_coinvestigate(max_coinvestigate=15) is True

    def test_can_start_coinvestigate_at_limit(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        _write_config(tmp_path, job_id, investigation_mode="coinvestigate")
        db_jobs = [
            _make_db_job(
                "running",
                "2026-02-01T00:00:00",
                job_id=job_id,
                investigation_mode="coinvestigate",
            )
        ]

        with patch(
            "openscientist.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=db_jobs
        ):
            assert manager.can_start_coinvestigate(max_coinvestigate=1) is False


class TestJobManagerCancellationConcurrency:
    """Tests for cancellation behavior and active-slot accounting."""

    def test_cancel_running_job_keeps_thread_registered_until_exit(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()

        running_job = JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=JobStatus.RUNNING,
            created_at="2026-02-01T00:00:00+00:00",
        )

        with (
            patch.object(manager, "get_job", return_value=running_job),
            patch.object(manager, "_update_job_status"),
            patch.object(manager, "_start_next_queued_job"),
        ):
            manager.cancel_job(job_id)

        # Cancellation is cooperative; keep tracking until worker thread exits.
        assert job_id in manager._running_jobs

    def test_active_job_count_keeps_cancelled_threads_until_exit(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()

        with patch(
            "openscientist.job_manager._db_get_job_statuses",
            new_callable=AsyncMock,
            return_value={job_id: JobStatus.CANCELLED.value},
        ):
            assert manager._get_active_job_count() == 1


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

    def test_create_job_blocks_when_budget_limit_exceeded(self, tmp_path):
        manager = _new_manager(tmp_path)
        fake_provider = SimpleNamespace(
            check_budget_limits=lambda: {
                "can_proceed": False,
                "errors": ["Provider budget exhausted"],
            }
        )

        with (
            patch.object(manager, "get_job", return_value=None),
            patch("openscientist.job_manager.get_provider", return_value=fake_provider),
            pytest.raises(ValueError, match="Cannot create job"),
        ):
            manager.create_job(
                job_id=str(uuid4()),
                research_question="Budget test",
                data_files=[],
                auto_start=False,
            )

    def test_create_job_rolls_back_database_when_filesystem_init_fails(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        fake_provider = SimpleNamespace(check_budget_limits=lambda: {"can_proceed": True})

        with (
            patch.object(manager, "get_job", return_value=None),
            patch("openscientist.job_manager.get_provider", return_value=fake_provider),
            patch("openscientist.job_manager._db_create_job", new_callable=AsyncMock),
            patch(
                "openscientist.job_manager._db_delete_job", new_callable=AsyncMock
            ) as mock_db_delete,
            patch("openscientist.job_manager.create_job", side_effect=RuntimeError("disk full")),
            pytest.raises(ValueError, match="Failed to initialize job files"),
        ):
            manager.create_job(
                job_id=job_id,
                research_question="Filesystem failure test",
                data_files=[],
                auto_start=False,
            )

        assert mock_db_delete.await_count == 1

    def test_create_job_records_llm_provider_and_model(self, tmp_path):
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
                claude_provider="Anthropic",
                anthropic_model=None,
                anthropic_default_sonnet_model="claude-sonnet-4-5",
            )
        )

        with (
            patch.object(manager, "get_job", return_value=None),
            patch.object(manager, "_check_budget_before_creation"),
            patch.object(manager, "_create_job_files"),
            patch.object(manager, "_load_job_info", return_value=created_job),
            patch("openscientist.settings.get_settings", return_value=settings),
            patch.object(manager, "_create_db_job_record") as mock_create_db_job_record,
        ):
            manager.create_job(
                job_id=job_id,
                research_question="Model tracking test",
                data_files=[],
                auto_start=False,
            )

        _, kwargs = mock_create_db_job_record.call_args
        assert kwargs["llm_provider"] == "anthropic"
        assert kwargs["llm_config"] == {"model": "claude-sonnet-4-5"}


class TestJobManagerCLI:
    """Tests for job_manager CLI argument handling."""

    def test_bootstrap_command_invokes_migration_without_legacy_flags(self, monkeypatch, capsys):
        """CLI bootstrap forwards only jobs dir and dry-run to migration entrypoint."""
        from openscientist.job_manager import main

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"created_jobs": 1, "orphan_jobs": 1}

        mock_bootstrap = MagicMock(return_value=mock_result)
        monkeypatch.setattr(
            "openscientist.bootstrap.bootstrap_jobs_from_filesystem_sync",
            mock_bootstrap,
        )
        monkeypatch.setattr(
            "sys.argv",
            ["job_manager.py", "bootstrap", "--jobs-dir", "jobs", "--dry-run"],
        )

        main()

        mock_bootstrap.assert_called_once_with(
            jobs_dir=Path("jobs"),
            dry_run=True,
        )
        stdout = capsys.readouterr().out
        assert '"created_jobs": 1' in stdout

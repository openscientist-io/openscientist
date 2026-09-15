"""Tests for job_manager module."""

import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from openscientist.job_manager import JobInfo, JobManager, JobStatus, JobStatusUpdateResult, RunMode


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


def _write_config(jobs_dir: Path, job_id: str, **overrides: Any) -> dict:
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
    **overrides: Any,
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


def _db_get_job_side_effect(
    models: list[SimpleNamespace],
) -> Callable[[str, str | None], Awaitable[SimpleNamespace | None]]:
    """Build side-effect that returns models by UUID id."""
    by_id = {m.id: m for m in models}

    async def _inner(job_id: str, user_id: str | None = None) -> SimpleNamespace | None:
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
    def manager(self, tmp_path: Path) -> JobManager:
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


class TestJobManagerRegenerateReport:
    """Tests for the admin report-regeneration path."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    @pytest.fixture
    def manager(self, tmp_path: Path) -> JobManager:
        return _new_manager(tmp_path)

    def test_missing_job_raises(self, manager):
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=None
        ):
            with pytest.raises(ValueError, match="not found"):
                manager.regenerate_report(str(uuid4()))

    def test_non_completed_raises(self, manager):
        db_job = _make_db_job("running", "2026-02-01T00:00:00")
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
        ):
            with pytest.raises(ValueError, match="not completed"):
                manager.regenerate_report(str(db_job.id))

    def test_already_running_raises(self, manager):
        db_job = _make_db_job("completed", "2026-02-01T00:00:00")
        manager._running_jobs[str(db_job.id)] = MagicMock()
        with patch(
            "openscientist.job_manager._db_get_job", new_callable=AsyncMock, return_value=db_job
        ):
            with pytest.raises(ValueError, match="already running"):
                manager.regenerate_report(str(db_job.id))

    def test_launches_in_report_only_mode(self, manager):
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
            manager.regenerate_report(job_id)

        assert manager._running_jobs[job_id] is fake_thread
        fake_thread.start.assert_called_once()
        # The worker targets _run_job in report-only mode (no discovery loop).
        kwargs = mock_thread.call_args.kwargs
        assert kwargs["target"] == manager._run_job
        assert kwargs["args"] == (job_id,)
        assert kwargs["kwargs"] == {"run_mode": RunMode.REPORT_ONLY}

    def test_regenerate_report_rejects_at_capacity(self, manager):
        """At max concurrency, regenerate must hard-fail and not queue or spawn."""
        db_job = _make_db_job("completed", "2026-02-01T00:00:00")
        job_id = str(db_job.id)

        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                return_value=db_job,
            ),
            patch.object(manager, "_get_active_job_count", return_value=manager.max_concurrent),
            patch("openscientist.job_manager.threading.Thread") as mock_thread,
        ):
            with pytest.raises(ValueError, match="maximum concurrent"):
                manager.regenerate_report(job_id)

        assert job_id not in manager._running_jobs
        mock_thread.assert_not_called()

    def test_regenerate_report_rejects_when_shutting_down(self, manager):
        """Shutdown drain must reject regenerate without registering a worker."""
        db_job = _make_db_job("completed", "2026-02-01T00:00:00")
        job_id = str(db_job.id)
        manager._shutting_down = True

        with (
            patch(
                "openscientist.job_manager._db_get_job",
                new_callable=AsyncMock,
                return_value=db_job,
            ),
            patch("openscientist.job_manager.threading.Thread") as mock_thread,
        ):
            with pytest.raises(ValueError, match="shutting down"):
                manager.regenerate_report(job_id)

        assert job_id not in manager._running_jobs
        mock_thread.assert_not_called()


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
        assert mock_update.await_args is not None
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

        assert mock_update.await_args is not None
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

        assert mock_update.await_args is not None
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


class TestJobManagerShutdown:
    """Tests for graceful shutdown of in-flight job threads."""

    def test_shutdown_noop_when_no_running_jobs(self, tmp_path):
        manager = _new_manager(tmp_path)
        manager.shutdown(timeout=1.0)
        assert manager._shutting_down is True

    def test_shutdown_cancels_and_stops_running_jobs(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())

        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = False
        manager._running_jobs[job_id] = fake_thread

        running_job = JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=JobStatus.RUNNING,
            created_at="2026-02-01T00:00:00+00:00",
        )

        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", return_value=running_job),
            patch.object(manager, "_update_job_status") as mock_update,
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            manager.shutdown(timeout=1.0)

        mock_update.assert_called_once_with(
            job_id,
            JobStatus.CANCELLED,
            cancellation_reason="Server shut down while job was running",
        )
        mock_runner.stop.assert_called_once_with(job_id)
        fake_thread.join.assert_called_once()
        # Thread exited cleanly within the timeout, no forced cleanup needed.
        mock_runner.cleanup.assert_not_called()

    def test_shutdown_stops_multiple_containers_concurrently(self, tmp_path):
        """JobContainerRunner.stop() blocks for up to its own 10s timeout per
        container. If shutdown() stopped containers one at a time, N
        concurrent jobs would take up to N times as long to signal, blowing
        past the shutdown timeout budget before the join phase even starts.
        Assert wall-clock time stays close to a single stop call, not the
        sum across jobs.
        """
        manager = _new_manager(tmp_path)
        job_ids = [str(uuid4()) for _ in range(4)]
        stop_delay = 0.25

        for job_id in job_ids:
            fake_thread = MagicMock()
            fake_thread.is_alive.return_value = False
            manager._running_jobs[job_id] = fake_thread

        running_jobs = {
            job_id: JobInfo(
                job_id=job_id,
                research_question="Q?",
                status=JobStatus.RUNNING,
                created_at="2026-02-01T00:00:00+00:00",
            )
            for job_id in job_ids
        }

        mock_runner = MagicMock()
        mock_runner.stop.side_effect = lambda _job_id: time.sleep(stop_delay)

        with (
            patch.object(manager, "get_job", side_effect=lambda jid: running_jobs[jid]),
            patch.object(manager, "_update_job_status"),
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            start = time.monotonic()
            manager.shutdown(timeout=5.0)
            elapsed = time.monotonic() - start

        assert mock_runner.stop.call_count == len(job_ids)
        # Sequential would take ~4 * stop_delay = 1.0s; concurrent stays near stop_delay.
        assert elapsed < stop_delay * 2, (
            f"shutdown() took {elapsed:.2f}s for {len(job_ids)} jobs; "
            f"looks sequential, not concurrent"
        )

    def test_shutdown_force_cleans_stuck_threads(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())

        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        manager._running_jobs[job_id] = fake_thread

        running_job = JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=JobStatus.RUNNING,
            created_at="2026-02-01T00:00:00+00:00",
        )

        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", return_value=running_job),
            patch.object(manager, "_update_job_status"),
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            manager.shutdown(timeout=1.0)

        mock_runner.cleanup.assert_called_once_with(job_id, log_dir=tmp_path / job_id)
        assert job_id not in manager._running_jobs

    def test_shutdown_cancels_awaiting_feedback_jobs(self, tmp_path):
        """A job awaiting feedback still has a live poll thread (only
        completed/failed/cancelled are terminal in the poll loop), so it must
        be cancelled and its container stopped too, not silently skipped."""
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())

        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = False
        manager._running_jobs[job_id] = fake_thread

        awaiting_job = JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=JobStatus.AWAITING_FEEDBACK,
            created_at="2026-02-01T00:00:00+00:00",
        )

        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", return_value=awaiting_job),
            patch.object(manager, "_update_job_status") as mock_update,
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            manager.shutdown(timeout=1.0)

        mock_update.assert_called_once_with(
            job_id,
            JobStatus.CANCELLED,
            cancellation_reason="Server shut down while job was running",
        )
        mock_runner.stop.assert_called_once_with(job_id)

    def test_shutdown_real_thread_wakes_up_and_exits(self, tmp_path):
        """Runs an actual background thread (not a MagicMock) that mirrors
        the shape of _run_job_in_container's poll loop, to prove shutdown()
        causes it to notice the cancellation and exit rather than just
        recording that join() was called. A shutdown() that only joined
        without ever signalling the job (like the ticket's own naive example)
        would hang here until the loop's bound and fail the "not alive"
        assertion.
        """
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())

        state = {"status": JobStatus.RUNNING}
        state_lock = threading.Lock()

        def fake_get_job(jid: str) -> JobInfo:
            with state_lock:
                status = state["status"]
            return JobInfo(
                job_id=jid,
                research_question="Q?",
                status=status,
                created_at="2026-02-01T00:00:00+00:00",
            )

        def fake_update_status(jid, status, error_message=None, cancellation_reason=None):
            with state_lock:
                state["status"] = status

        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        shutdown_timeout = 2.0

        def poll_loop() -> None:
            # Bound is well past shutdown_timeout: if shutdown() were broken
            # (e.g. joins without ever signalling cancellation, like the
            # ticket's own naive example), this loop would still be alive
            # when shutdown()'s join(timeout=shutdown_timeout) gives up,
            # so the "not alive" assertion below would correctly fail
            # instead of the test passing by coincidence of this bound.
            for _ in range(500):  # 500 * 0.02s = 10s, 5x shutdown_timeout
                if fake_get_job(job_id).status in terminal:
                    return
                time.sleep(0.02)

        real_thread = threading.Thread(target=poll_loop, daemon=True)
        manager._running_jobs[job_id] = real_thread
        real_thread.start()

        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", side_effect=fake_get_job),
            patch.object(manager, "_update_job_status", side_effect=fake_update_status),
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            manager.shutdown(timeout=shutdown_timeout)

        assert not real_thread.is_alive()
        mock_runner.stop.assert_called_once_with(job_id)

    def test_shutdown_blocks_new_job_starts(self, tmp_path):
        manager = _new_manager(tmp_path)
        manager.shutdown(timeout=1.0)

        with pytest.raises(ValueError, match="shutting down"):
            manager.start_job(str(uuid4()))


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
                provider_id="Anthropic",
                model=None,
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


class TestEffectiveModel:
    """`_effective_model` records the model the active provider will actually
    use, so the job can show a model badge."""

    @staticmethod
    def _settings(
        model: str | None = None, anthropic_default: str | None = None
    ) -> SimpleNamespace:
        return SimpleNamespace(
            provider=SimpleNamespace(
                model=model,
                anthropic_default_sonnet_model=anthropic_default,
            )
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


class TestJobManagerCancelSummaryCoverage:
    """Coverage for cancel_job branches, active-count, and job summary (Priority-7)."""

    @pytest.fixture(autouse=True)
    def _mock_ks_progress(self):
        with patch(
            "openscientist.job_manager._load_progress_from_knowledge_state",
            return_value=(0, 0),
        ):
            yield

    def _job(self, status: JobStatus, job_id: str = "j1") -> JobInfo:
        return JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=status,
            created_at="2026-01-01T00:00:00+00:00",
        )

    def test_cancel_nonexistent_raises(self, tmp_path):
        manager = _new_manager(tmp_path)
        with patch.object(manager, "get_job", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                manager.cancel_job(str(uuid4()))

    @pytest.mark.parametrize(
        "status",
        [
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.AWAITING_FEEDBACK,
            JobStatus.GENERATING_REPORT,
        ],
    )
    def test_cancel_rejects_non_cancellable_status(self, tmp_path: Path, status: JobStatus) -> None:
        """cancel_job only accepts pending/queued/running; other statuses raise."""
        manager = _new_manager(tmp_path)
        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", return_value=self._job(status)),
            patch.object(manager, "_update_job_status") as mock_update,
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            with pytest.raises(ValueError, match="not pending, running, or queued"):
                manager.cancel_job("j1")

        mock_update.assert_not_called()
        mock_runner.stop.assert_not_called()

    def test_cancel_pending_untracks_without_container_stop(self, tmp_path):
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()
        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", return_value=self._job(JobStatus.PENDING, job_id)),
            patch.object(manager, "_update_job_status") as mock_update,
            patch.object(manager, "_start_next_queued_job") as mock_start_next,
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            manager.cancel_job(job_id)
        assert mock_update.call_args.args[1] == JobStatus.CANCELLED
        assert mock_update.call_args.kwargs["cancellation_reason"] == "Cancelled by user"
        assert job_id not in manager._running_jobs
        mock_runner.stop.assert_not_called()
        mock_start_next.assert_called_once_with()

    def test_cancel_queued_untracks_without_container_stop(self, tmp_path):
        """Queued cancel marks cancelled, untracks the slot, and does not stop a container."""
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()
        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", return_value=self._job(JobStatus.QUEUED, job_id)),
            patch.object(manager, "_update_job_status") as mock_update,
            patch.object(manager, "_start_next_queued_job") as mock_start_next,
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            manager.cancel_job(job_id)
        assert mock_update.call_args.args[1] == JobStatus.CANCELLED
        assert mock_update.call_args.kwargs["cancellation_reason"] == "Cancelled by user"
        assert job_id not in manager._running_jobs
        mock_runner.stop.assert_not_called()
        mock_start_next.assert_called_once_with()

    def test_cancel_running_invokes_container_stop(self, tmp_path):
        """Running cancel keeps thread accounting and immediately stops the agent container."""
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()
        mock_runner = MagicMock()
        with (
            patch.object(manager, "get_job", return_value=self._job(JobStatus.RUNNING, job_id)),
            patch.object(manager, "_update_job_status") as mock_update,
            patch.object(manager, "_start_next_queued_job") as mock_start_next,
            patch("openscientist.job_container.JobContainerRunner", return_value=mock_runner),
        ):
            manager.cancel_job(job_id)

        mock_update.assert_called_once_with(
            job_id,
            JobStatus.CANCELLED,
            cancellation_reason="Cancelled by user",
        )
        assert job_id in manager._running_jobs
        mock_runner.stop.assert_called_once_with(job_id)
        mock_start_next.assert_called_once_with()

    def test_active_job_count_zero_when_none_running(self, tmp_path):
        manager = _new_manager(tmp_path)
        assert manager._get_active_job_count() == 0

    def test_list_operational_jobs_returns_empty_on_db_error(self, tmp_path):
        manager = _new_manager(tmp_path)
        with patch(
            "openscientist.job_manager._db_list_jobs",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            assert manager._list_operational_jobs() == []

    def test_get_job_summary_counts_and_cost(self, tmp_path):
        import json
        from datetime import UTC, datetime

        from openscientist.providers.base import CostInfo

        manager = _new_manager(tmp_path)
        jobs = [self._job(JobStatus.COMPLETED, "a"), self._job(JobStatus.FAILED, "b")]
        cost_info = CostInfo(
            provider_name="stub",
            total_spend_usd=10.0,
            recent_spend_usd=2.5,
            recent_period_hours=24,
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            metadata={"source": "test"},
        )
        provider = MagicMock()
        provider.get_cost_info.return_value = cost_info
        provider.evaluate_budget.return_value = "budget"
        with (
            patch.object(manager, "list_jobs", return_value=jobs),
            patch("openscientist.job_manager.get_provider", return_value=provider),
        ):
            summary = manager.get_job_summary()
        assert summary["total_jobs"] == 2
        assert summary["status_counts"]["completed"] == 1
        assert summary["status_counts"]["failed"] == 1
        assert isinstance(summary["cost_info"], dict)
        assert summary["cost_info"]["provider_name"] == "stub"
        assert summary["cost_info"]["total_spend_usd"] == 10.0
        assert summary["cost_info"]["recent_spend_usd"] == 2.5
        assert summary["cost_info"]["last_updated"] == "2026-01-15T12:00:00+00:00"
        assert summary["cost_info"]["metadata"] == {"source": "test"}
        assert summary["budget_check"] == "budget"
        # Regression: summary must be JSON-serializable with a real CostInfo
        json.dumps(summary)

    def test_get_job_summary_handles_provider_error(self, tmp_path):
        from openscientist.exceptions import ProviderError

        manager = _new_manager(tmp_path)
        with (
            patch.object(manager, "list_jobs", return_value=[]),
            patch(
                "openscientist.job_manager.get_provider",
                side_effect=ProviderError("no cost"),
            ),
        ):
            summary = manager.get_job_summary()
        assert summary["total_jobs"] == 0
        assert summary["cost_info"] is None
        assert summary["budget_check"] is None


class TestRunJobInContainer:
    """Cover container poll/failure/cleanup paths in _run_job_in_container."""

    def _running_info(self, job_id: str) -> JobInfo:
        return JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=JobStatus.RUNNING,
            created_at="2026-01-01T00:00:00+00:00",
        )

    def test_reaches_terminal_status_and_cleans_up(self, tmp_path: Path) -> None:
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        (tmp_path / job_id).mkdir()
        manager._running_jobs[job_id] = MagicMock()

        runner = MagicMock()
        runner.get_exit_code.return_value = None
        completed = JobInfo(
            job_id=job_id,
            research_question="Q?",
            status=JobStatus.COMPLETED,
            created_at="2026-01-01T00:00:00+00:00",
        )

        with (
            patch("openscientist.job_container.JobContainerRunner", return_value=runner),
            patch.object(manager, "_update_job_status"),
            patch.object(manager, "_load_job_info", return_value=completed),
            patch.object(manager, "_start_next_queued_job") as mock_next,
            patch("openscientist.job_manager.time.sleep"),
            patch(
                "openscientist.settings.get_settings",
                return_value=SimpleNamespace(container=SimpleNamespace(agent_timeout=30)),
            ),
        ):
            manager._run_job_in_container(job_id)

        runner.launch.assert_called_once()
        runner.cleanup.assert_called_once()
        assert job_id not in manager._running_jobs
        mock_next.assert_called_once()

    def test_nonzero_container_exit_marks_failed(self, tmp_path: Path) -> None:
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        (tmp_path / job_id).mkdir()
        manager._running_jobs[job_id] = MagicMock()

        runner = MagicMock()
        runner.get_exit_code.return_value = 7

        with (
            patch("openscientist.job_container.JobContainerRunner", return_value=runner),
            patch.object(manager, "_update_job_status") as mock_update,
            patch.object(manager, "_load_job_info", return_value=self._running_info(job_id)),
            patch.object(manager, "_start_next_queued_job"),
            patch("openscientist.job_manager.time.sleep"),
            patch(
                "openscientist.settings.get_settings",
                return_value=SimpleNamespace(container=SimpleNamespace(agent_timeout=30)),
            ),
        ):
            manager._run_job_in_container(job_id)

        failed_calls = [
            c
            for c in mock_update.call_args_list
            if len(c.args) >= 2 and c.args[1] == JobStatus.FAILED
        ]
        assert failed_calls
        assert "exited with code 7" in failed_calls[0].kwargs.get("error_message", "")
        runner.cleanup.assert_called_once()

    def test_timeout_marks_failed(self, tmp_path: Path) -> None:
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        (tmp_path / job_id).mkdir()
        manager._running_jobs[job_id] = MagicMock()

        runner = MagicMock()
        runner.get_exit_code.return_value = None

        with (
            patch("openscientist.job_container.JobContainerRunner", return_value=runner),
            patch.object(manager, "_update_job_status") as mock_update,
            patch.object(manager, "_load_job_info", return_value=self._running_info(job_id)),
            patch.object(manager, "_start_next_queued_job"),
            patch("openscientist.job_manager.time.sleep"),
            patch(
                "openscientist.settings.get_settings",
                return_value=SimpleNamespace(container=SimpleNamespace(agent_timeout=5)),
            ),
        ):
            # poll_interval is 5; after one sleep elapsed >= timeout
            manager._run_job_in_container(job_id)

        failed_calls = [
            c
            for c in mock_update.call_args_list
            if len(c.args) >= 2 and c.args[1] == JobStatus.FAILED
        ]
        assert any("timed out" in (c.kwargs.get("error_message") or "") for c in failed_calls)
        runner.cleanup.assert_called_once()

    def test_launch_exception_marks_failed_and_cleans_up(self, tmp_path: Path) -> None:
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        (tmp_path / job_id).mkdir()
        manager._running_jobs[job_id] = MagicMock()

        runner = MagicMock()
        runner.launch.side_effect = RuntimeError("docker down")

        with (
            patch("openscientist.job_container.JobContainerRunner", return_value=runner),
            patch.object(manager, "_update_job_status") as mock_update,
            patch.object(manager, "_start_next_queued_job"),
        ):
            manager._run_job_in_container(job_id)

        failed_calls = [
            c
            for c in mock_update.call_args_list
            if len(c.args) >= 2 and c.args[1] == JobStatus.FAILED
        ]
        assert failed_calls
        assert "docker down" in failed_calls[0].kwargs.get("error_message", "")
        runner.cleanup.assert_called_once()
        assert job_id not in manager._running_jobs

    def test_start_job_not_found_and_already_running(self, tmp_path: Path) -> None:
        manager = _new_manager(tmp_path)
        with patch.object(manager, "get_job", return_value=None):
            with pytest.raises(ValueError, match="not found"):
                manager.start_job("missing")

        job_id = str(uuid4())
        manager._running_jobs[job_id] = MagicMock()
        with patch.object(
            manager,
            "get_job",
            return_value=JobInfo(
                job_id=job_id,
                research_question="Q?",
                status=JobStatus.PENDING,
                created_at="2026-01-01T00:00:00+00:00",
            ),
        ):
            with pytest.raises(ValueError, match="already running"):
                manager.start_job(job_id)

    def test_start_job_queues_when_at_capacity(self, tmp_path: Path) -> None:
        manager = _new_manager(tmp_path)
        job_id = str(uuid4())
        with (
            patch.object(
                manager,
                "get_job",
                return_value=JobInfo(
                    job_id=job_id,
                    research_question="Q?",
                    status=JobStatus.PENDING,
                    created_at="2026-01-01T00:00:00+00:00",
                ),
            ),
            patch.object(manager, "_get_active_job_count", return_value=manager.max_concurrent),
            patch.object(manager, "_update_job_status") as mock_update,
        ):
            manager.start_job(job_id)
        mock_update.assert_called_with(job_id, JobStatus.QUEUED)

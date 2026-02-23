"""Tests for job_manager module."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from shandy.job_manager import JobInfo, JobManager, JobStatus

# ─── JobStatus enum ──────────────────────────────────────────────────


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


# ─── JobInfo dataclass ───────────────────────────────────────────────


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
        assert d["status"] == "completed"  # string, not enum
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


# ─── JobManager ──────────────────────────────────────────────────────


def _write_config(jobs_dir: Path, job_id: str, **overrides):
    """Helper: write a minimal config.json for a job."""
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "job_id": job_id,
        "research_question": "Test?",
        "status": "created",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "max_iterations": 10,
        "use_skills": True,
        "investigation_mode": "autonomous",
        **overrides,
    }
    (job_dir / "config.json").write_text(json.dumps(config))
    return config


class TestJobManagerInit:
    """Tests for JobManager initialization."""

    def test_creates_jobs_dir(self, tmp_path):
        d = tmp_path / "myjobs"
        assert not d.exists()
        JobManager(jobs_dir=d)
        assert d.exists()

    def test_cleans_stale_running_jobs(self, tmp_path):
        _write_config(tmp_path, "stale1", status="running")
        _write_config(tmp_path, "stale2", status="queued")
        _write_config(tmp_path, "ok", status="completed")

        JobManager(jobs_dir=tmp_path)

        with open(tmp_path / "stale1" / "config.json", encoding="utf-8") as f:
            assert json.load(f)["status"] == "cancelled"
        with open(tmp_path / "stale2" / "config.json", encoding="utf-8") as f:
            assert json.load(f)["status"] == "cancelled"
        with open(tmp_path / "ok" / "config.json", encoding="utf-8") as f:
            assert json.load(f)["status"] == "completed"


def _make_db_job(status: str, created_at: str, **overrides) -> SimpleNamespace:
    """Helper: create a fake DB job model for list_jobs tests."""
    defaults = {
        "id": uuid4(),
        "title": "Test?",
        "status": status,
        "created_at": datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc),
        "updated_at": datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc),
        "max_iterations": 10,
        "current_iteration": 0,
        "error_message": None,
        "cancellation_reason": None,
        "owner_id": None,
        "short_title": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestJobManagerListAndGet:
    """Tests for listing and getting jobs."""

    @pytest.fixture
    def db_jobs(self):
        """Fake DB models returned by _db_list_jobs (newest first)."""
        return [
            _make_db_job("completed", "2026-02-03T00:00:00"),
            _make_db_job("failed", "2026-02-02T00:00:00"),
            _make_db_job("completed", "2026-02-01T00:00:00"),
        ]

    @pytest.fixture
    def manager(self, tmp_path) -> JobManager:
        # Config files for get_job fallback
        _write_config(tmp_path, "j1", status="completed", created_at="2026-02-01T00:00:00")
        _write_config(tmp_path, "j2", status="failed", created_at="2026-02-02T00:00:00")
        _write_config(tmp_path, "j3", status="completed", created_at="2026-02-03T00:00:00")
        return JobManager(jobs_dir=tmp_path)

    def test_list_all_jobs(self, manager, db_jobs):
        with patch(
            "shandy.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=db_jobs
        ):
            jobs = manager.list_jobs()
        assert len(jobs) == 3

    def test_list_sorted_newest_first(self, manager, db_jobs):
        with patch(
            "shandy.job_manager._db_list_jobs", new_callable=AsyncMock, return_value=db_jobs
        ):
            jobs = manager.list_jobs()
        assert jobs[0].created_at > jobs[-1].created_at

    def test_list_passes_status_filter(self, manager, db_jobs):
        with patch(
            "shandy.job_manager._db_list_jobs",
            new_callable=AsyncMock,
            return_value=[db_jobs[1]],
        ) as mock:
            jobs = manager.list_jobs(status=JobStatus.FAILED)
            mock.assert_called_once_with(status=JobStatus.FAILED, limit=None)
        assert len(jobs) == 1

    def test_list_passes_limit(self, manager, db_jobs):
        with patch(
            "shandy.job_manager._db_list_jobs",
            new_callable=AsyncMock,
            return_value=db_jobs[:2],
        ) as mock:
            jobs = manager.list_jobs(limit=2)
            mock.assert_called_once_with(status=None, limit=2)
        assert len(jobs) == 2

    def test_get_existing_job(self, manager):
        job = manager.get_job("j1")
        assert job is not None
        assert job.status == JobStatus.COMPLETED

    def test_get_nonexistent_returns_none(self, manager):
        assert manager.get_job("no_such_job") is None


class TestJobManagerDelete:
    """Tests for job deletion."""

    def test_delete_completed_job(self, tmp_path):
        _write_config(tmp_path, "j1", status="completed")
        manager = JobManager(jobs_dir=tmp_path)
        manager.delete_job("j1")
        assert not (tmp_path / "j1").exists()

    def test_delete_nonexistent_raises(self, tmp_path):
        manager = JobManager(jobs_dir=tmp_path)
        with pytest.raises(ValueError, match="not found"):
            manager.delete_job("nope")

    def test_delete_running_raises(self, tmp_path):
        _write_config(tmp_path, "j1", status="running")
        manager = JobManager(jobs_dir=tmp_path)
        # After init, stale running jobs are cancelled, so re-create
        _write_config(tmp_path, "j2", status="completed")
        # Manually set a running state after manager init
        _write_config(tmp_path, "j3", status="running")
        # The manager didn't clean this one because it was created after init
        with pytest.raises(ValueError, match="Cannot delete running"):
            manager.delete_job("j3")


class TestJobManagerStatusUpdate:
    """Tests for status update logic."""

    def test_update_status_writes_timestamp(self, tmp_path):
        _write_config(tmp_path, "j1", status="created")
        manager = JobManager(jobs_dir=tmp_path)
        manager._update_job_status("j1", JobStatus.RUNNING)

        with open(tmp_path / "j1" / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        assert config["status"] == "running"
        assert "started_at" in config

    def test_completed_writes_completed_at(self, tmp_path):
        _write_config(tmp_path, "j1", status="created")
        manager = JobManager(jobs_dir=tmp_path)
        manager._update_job_status("j1", JobStatus.COMPLETED)

        with open(tmp_path / "j1" / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        assert "completed_at" in config

    def test_failed_writes_failed_at(self, tmp_path):
        _write_config(tmp_path, "j1", status="created")
        manager = JobManager(jobs_dir=tmp_path)
        manager._update_job_status("j1", JobStatus.FAILED)

        with open(tmp_path / "j1" / "config.json", encoding="utf-8") as f:
            config = json.load(f)
        assert "failed_at" in config


class TestJobManagerKSProgress:
    """Tests for real-time KS progress reading."""

    def test_running_job_reads_ks(self, tmp_path):
        _write_config(tmp_path, "j1", status="running")
        # Create a knowledge_state.json with iteration=5 and 2 findings
        ks_data = {
            "iteration": 5,
            "findings": [{"id": "F001"}, {"id": "F002"}],
        }
        (tmp_path / "j1" / "knowledge_state.json").write_text(json.dumps(ks_data))

        manager = JobManager(jobs_dir=tmp_path)
        # Manager marks stale running as cancelled, but let's reset
        _write_config(tmp_path, "j1", status="running")
        (tmp_path / "j1" / "knowledge_state.json").write_text(json.dumps(ks_data))

        job = manager.get_job("j1")
        assert job.iterations_completed == 4  # iteration 5 means 4 completed
        assert job.findings_count == 2


class TestJobManagerCoinvestigate:
    """Tests for co-investigate mode helpers."""

    def test_coinvestigate_count(self, tmp_path):
        _write_config(tmp_path, "c1", status="completed", investigation_mode="coinvestigate")
        _write_config(
            tmp_path,
            "c2",
            status="awaiting_feedback",
            investigation_mode="coinvestigate",
        )
        _write_config(tmp_path, "a1", status="completed", investigation_mode="autonomous")
        manager = JobManager(jobs_dir=tmp_path)

        # c1 is completed (doesn't count), c2 was awaiting_feedback but got
        # cancelled by stale cleanup. So count should be 0.
        assert manager.get_coinvestigate_count() == 0

    def test_can_start_coinvestigate_under_limit(self, tmp_path):
        manager = JobManager(jobs_dir=tmp_path)
        assert manager.can_start_coinvestigate(max_coinvestigate=15) is True

    def test_can_start_coinvestigate_at_limit(self, tmp_path):
        # Create many coinvestigate jobs in awaiting state
        # (they'll be cancelled by stale cleanup, but let's test the logic)
        manager = JobManager(jobs_dir=tmp_path)
        assert manager.can_start_coinvestigate(max_coinvestigate=0) is False


class TestJobManagerCleanup:
    """Tests for old job cleanup."""

    def test_cleanup_old_jobs(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _write_config(tmp_path, "old", status="failed", created_at=old_date)
        _write_config(tmp_path, "new", status="failed")

        manager = JobManager(jobs_dir=tmp_path)
        deleted = manager.cleanup_old_jobs(days=7, keep_completed=True)
        assert deleted == 1
        assert not (tmp_path / "old").exists()
        assert (tmp_path / "new").exists()

    def test_cleanup_keeps_completed(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _write_config(tmp_path, "old_done", status="completed", created_at=old_date)

        manager = JobManager(jobs_dir=tmp_path)
        deleted = manager.cleanup_old_jobs(days=7, keep_completed=True)
        assert deleted == 0
        assert (tmp_path / "old_done").exists()


class TestJobManagerCreationSafety:
    """Tests for creation-time safety guards."""

    def test_create_job_blocks_when_budget_limit_exceeded(self, tmp_path):
        manager = JobManager(jobs_dir=tmp_path)
        fake_provider = SimpleNamespace(
            check_budget_limits=lambda: {
                "can_proceed": False,
                "errors": ["Provider budget exhausted"],
            }
        )

        with (
            patch.object(manager, "get_job", return_value=None),
            patch("shandy.job_manager.get_provider", return_value=fake_provider),
        ):
            with pytest.raises(ValueError, match="Cannot create job"):
                manager.create_job(
                    job_id=str(uuid4()),
                    research_question="Budget test",
                    data_files=[],
                    auto_start=False,
                )

    def test_create_job_rolls_back_database_when_filesystem_init_fails(self, tmp_path):
        manager = JobManager(jobs_dir=tmp_path)
        job_id = str(uuid4())
        fake_provider = SimpleNamespace(check_budget_limits=lambda: {"can_proceed": True})

        with (
            patch.object(manager, "get_job", return_value=None),
            patch("shandy.job_manager.get_provider", return_value=fake_provider),
            patch("shandy.job_manager._db_create_job", new_callable=AsyncMock),
            patch("shandy.job_manager._db_delete_job", new_callable=AsyncMock) as mock_db_delete,
            patch("shandy.job_manager.create_job", side_effect=RuntimeError("disk full")),
        ):
            with pytest.raises(ValueError, match="Failed to initialize job files"):
                manager.create_job(
                    job_id=job_id,
                    research_question="Filesystem failure test",
                    data_files=[],
                    auto_start=False,
                )

        assert mock_db_delete.await_count == 1

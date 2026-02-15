"""Tests for orchestrator helper functions.

Only tests pure/helper functions that don't spawn subprocesses or require
the full agent loop. The run_discovery integration is too heavyweight for
unit testing.
"""

import json
import os
from unittest.mock import MagicMock, patch

from shandy.orchestrator import (
    get_version_metadata,
    increment_ks_iteration,
    update_job_status,
)

# ─── get_version_metadata ─────────────────────────────────────────────


class TestGetVersionMetadata:
    """Tests for version metadata collection."""

    @patch.dict(
        os.environ,
        {"SHANDY_COMMIT": "abc123def456", "SHANDY_BUILD_TIME": "2026-02-01T00:00:00"},
    )
    def test_from_env_vars(self):
        info = get_version_metadata()
        assert info["shandy_commit"] == "abc123def456"
        assert info["shandy_build_time"] == "2026-02-01T00:00:00"

    @patch.dict(os.environ, {"SHANDY_COMMIT": "unknown"}, clear=False)
    @patch("shandy.orchestrator.subprocess.run")
    def test_falls_back_to_git(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="abcdef123456789\n")
        # Remove build time so it doesn't appear
        with patch.dict(os.environ, {"SHANDY_BUILD_TIME": "unknown"}):
            info = get_version_metadata()
        assert info.get("shandy_commit", "").startswith("abcdef12")

    @patch.dict(os.environ, {}, clear=True)
    @patch("shandy.orchestrator.subprocess.run", side_effect=FileNotFoundError)
    @patch("shandy.orchestrator.Path")
    def test_empty_when_no_info_available(self, mock_path_cls, mock_run):
        mock_path_cls.return_value.exists.return_value = False
        info = get_version_metadata()
        assert isinstance(info, dict)


# ─── update_job_status ────────────────────────────────────────────────


class TestUpdateJobStatus:
    """Tests for job status file updates."""

    def test_update_status(self, tmp_path):
        config = {"job_id": "j1", "status": "created"}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))

        update_job_status(tmp_path, "running")

        with open(config_path, encoding="utf-8") as f:
            updated = json.load(f)
        assert updated["status"] == "running"

    def test_awaiting_feedback_adds_timestamp(self, tmp_path):
        config = {"job_id": "j1", "status": "running"}
        (tmp_path / "config.json").write_text(json.dumps(config))

        update_job_status(tmp_path, "awaiting_feedback")

        with open(tmp_path / "config.json", encoding="utf-8") as f:
            updated = json.load(f)
        assert "awaiting_feedback_since" in updated

    def test_leaving_awaiting_feedback_removes_timestamp(self, tmp_path):
        config = {
            "job_id": "j1",
            "status": "awaiting_feedback",
            "awaiting_feedback_since": "2026-01-01T00:00:00",
        }
        (tmp_path / "config.json").write_text(json.dumps(config))

        update_job_status(tmp_path, "running")

        with open(tmp_path / "config.json", encoding="utf-8") as f:
            updated = json.load(f)
        assert "awaiting_feedback_since" not in updated


# ─── increment_ks_iteration ──────────────────────────────────────────


class TestIncrementKsIteration:
    """Tests for atomic iteration increment."""

    def test_increments_iteration(self, tmp_path):
        ks_path = tmp_path / "knowledge_state.json"
        ks_path.write_text(json.dumps({"iteration": 3, "findings": []}))

        increment_ks_iteration(ks_path)

        with open(ks_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["iteration"] == 4

    def test_preserves_other_fields(self, tmp_path):
        ks_path = tmp_path / "knowledge_state.json"
        ks_path.write_text(
            json.dumps({"iteration": 1, "findings": [{"id": "F001"}], "hypotheses": []})
        )

        increment_ks_iteration(ks_path)

        with open(ks_path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["findings"]) == 1
        assert data["hypotheses"] == []


# ─── create_job ───────────────────────────────────────────────────────


class TestCreateJob:
    """Tests for job creation."""

    def test_creates_job_directory(self, tmp_path):
        from shandy.orchestrator import create_job

        data_file = tmp_path / "test.csv"
        data_file.write_text("a,b\n1,2\n")

        job_dir = create_job(
            job_id="test_123",
            research_question="Why?",
            data_files=[data_file],
            max_iterations=5,
            jobs_dir=tmp_path,
        )

        assert job_dir.exists()
        assert (job_dir / "config.json").exists()
        assert (job_dir / "knowledge_state.json").exists()
        assert (job_dir / "data").is_dir()
        assert (job_dir / "provenance").is_dir()

    def test_config_contents(self, tmp_path):
        from shandy.orchestrator import create_job

        data_file = tmp_path / "test.csv"
        data_file.write_text("a,b\n1,2\n")

        job_dir = create_job(
            job_id="test_456",
            research_question="What is X?",
            data_files=[data_file],
            max_iterations=15,
            use_skills=False,
            jobs_dir=tmp_path,
            investigation_mode="coinvestigate",
        )

        with open(job_dir / "config.json", encoding="utf-8") as f:
            config = json.load(f)

        assert config["job_id"] == "test_456"
        assert config["research_question"] == "What is X?"
        assert config["max_iterations"] == 15
        assert config["use_skills"] is False
        assert config["investigation_mode"] == "coinvestigate"
        assert config["status"] == "created"

    def test_copies_data_file(self, tmp_path):
        from shandy.orchestrator import create_job

        data_file = tmp_path / "input_data.csv"
        data_file.write_text("x,y\n1,2\n3,4\n")

        job_dir = create_job(
            job_id="copy_test",
            research_question="Q?",
            data_files=[data_file],
            max_iterations=5,
            jobs_dir=tmp_path,
        )

        copied = job_dir / "data" / "input_data.csv"
        assert copied.exists()
        assert copied.read_text() == "x,y\n1,2\n3,4\n"

    def test_no_data_files(self, tmp_path):
        from shandy.orchestrator import create_job

        job_dir = create_job(
            job_id="no_data",
            research_question="Literature only?",
            data_files=[],
            max_iterations=5,
            jobs_dir=tmp_path,
        )

        with open(job_dir / "knowledge_state.json", encoding="utf-8") as f:
            ks = json.load(f)
        assert ks["data_summary"]["files"] == []
        assert ks["data_summary"]["file_type"] == "none"

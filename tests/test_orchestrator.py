"""Tests for orchestrator helper functions.

Only tests pure/helper functions that don't spawn subprocesses or require
the full agent loop. The run_discovery integration is too heavyweight for
unit testing.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

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
    @patch("shandy.version._commit", None)
    @patch("subprocess.run")
    def test_falls_back_to_git(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="abcdef123456789\n")
        # Remove build time so it doesn't appear
        with patch.dict(os.environ, {"SHANDY_BUILD_TIME": "unknown"}):
            info = get_version_metadata()
        assert info.get("shandy_commit", "").startswith("abcdef12")

    @patch.dict(os.environ, {}, clear=True)
    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("shandy.orchestrator.discovery.Path")
    def test_empty_when_no_info_available(self, mock_path_cls, _mock_run):
        mock_path_cls.return_value.exists.return_value = False
        info = get_version_metadata()
        assert isinstance(info, dict)


# ─── update_job_status ────────────────────────────────────────────────


class TestUpdateJobStatus:
    """Tests for DB-backed job status updates."""

    @pytest.mark.asyncio
    async def test_update_status(self, tmp_path):
        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        job = MagicMock()
        job.status = "pending"
        job.owner_id = None
        job.short_title = None
        job.title = "Test job"

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        with patch("shandy.orchestrator.iteration.AsyncSessionLocal") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=job_result)
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value = mock_session
            mock_cm.__aexit__.return_value = False
            mock_session_cls.return_value = mock_cm

            await update_job_status(job_dir, "running")

        assert job.status == "running"
        mock_session.flush.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_awaiting_feedback_sends_notification(self, tmp_path):
        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "knowledge_state.json").write_text(json.dumps({"iteration": 4}))

        owner_id = uuid4()
        job = MagicMock()
        job.status = "running"
        job.owner_id = owner_id
        job.short_title = "Short title"
        job.title = "Long title"

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        user_row = MagicMock()
        user_row.ntfy_enabled = True
        user_row.ntfy_topic = "topic-123"
        user_result = MagicMock()
        user_result.first.return_value = user_row

        with (
            patch("shandy.orchestrator.iteration.AsyncSessionLocal") as mock_session_cls,
            patch(
                "shandy.orchestrator.iteration.notify_job_status_change",
                new_callable=AsyncMock,
            ) as mock_notify,
        ):
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(side_effect=[job_result, user_result])
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value = mock_session
            mock_cm.__aexit__.return_value = False
            mock_session_cls.return_value = mock_cm

            await update_job_status(job_dir, "awaiting_feedback")

        mock_notify.assert_awaited_once()
        kwargs = mock_notify.await_args.kwargs
        assert kwargs["job_id"] == job_id
        assert kwargs["job_title"] == "Short title"
        assert kwargs["new_status"] == "awaiting_feedback"
        assert kwargs["iteration"] == 4

    @pytest.mark.asyncio
    async def test_running_does_not_send_feedback_notification(self, tmp_path):
        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        job = MagicMock()
        job.status = "awaiting_feedback"
        job.owner_id = uuid4()
        job.short_title = "Short"
        job.title = "Title"

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        user_row = MagicMock()
        user_row.ntfy_enabled = True
        user_row.ntfy_topic = "topic-123"
        user_result = MagicMock()
        user_result.first.return_value = user_row

        with (
            patch("shandy.orchestrator.iteration.AsyncSessionLocal") as mock_session_cls,
            patch(
                "shandy.orchestrator.iteration.notify_job_status_change",
                new_callable=AsyncMock,
            ) as mock_notify,
        ):
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(side_effect=[job_result, user_result])
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value = mock_session
            mock_cm.__aexit__.return_value = False
            mock_session_cls.return_value = mock_cm

            await update_job_status(job_dir, "running")

        mock_notify.assert_not_awaited()


# ─── discovery cancellation / failure flow ────────────────────────────


class TestDiscoveryCancellationAndFailure:
    """Regression tests for cancellation and iteration-failure handling."""

    @pytest.mark.asyncio
    async def test_cancelled_feedback_wait_does_not_resume_running(self, tmp_path):
        from shandy.orchestrator.discovery import _wait_for_coinvestigate_feedback

        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        wait_outcome = {
            "outcome": "cancelled",
            "feedback_text": None,
        }

        with (
            patch(
                "shandy.orchestrator.discovery.update_job_status", new_callable=AsyncMock
            ) as mock_update,
            patch(
                "shandy.orchestrator.discovery.wait_for_feedback_or_timeout",
                new_callable=AsyncMock,
                return_value=wait_outcome,
            ),
        ):
            result = await _wait_for_coinvestigate_feedback(
                job_dir=job_dir,
                investigation_mode="coinvestigate",
                current_iteration=1,
                max_iterations=4,
            )

        assert result == wait_outcome
        # Should enter awaiting_feedback but must not flip back to running when cancelled.
        assert mock_update.await_count == 1
        assert mock_update.await_args.args == (job_dir, "awaiting_feedback")

    @pytest.mark.asyncio
    async def test_run_discovery_stops_when_cancelled_before_next_iteration(self, tmp_path):
        from shandy.agent.protocol import IterationResult, TokenUsage
        from shandy.orchestrator.discovery import run_discovery_async

        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        from shandy.knowledge_state import KnowledgeState

        ks = KnowledgeState(job_id, "Question?", 3)
        ks.save(job_dir / "knowledge_state.json")

        runtime = {
            "job_id": job_id,
            "research_question": "Question?",
            "max_iterations": 3,
            "use_skills": False,
            "investigation_mode": "autonomous",
            "data_files": [],
        }

        mock_executor = MagicMock()
        mock_executor.total_tokens = TokenUsage()
        mock_executor.shutdown = AsyncMock()
        mock_executor.run_iteration = AsyncMock(
            side_effect=[
                IterationResult(
                    success=True,
                    output="iteration 1 complete",
                    tool_calls=0,
                    transcript=[],
                ),
                AssertionError("second iteration should not run after cancellation"),
            ]
        )

        mock_provider = MagicMock()

        with (
            patch(
                "shandy.orchestrator.discovery._load_runtime_context",
                new_callable=AsyncMock,
                return_value=runtime,
            ),
            patch("shandy.orchestrator.discovery.get_provider", return_value=mock_provider),
            patch(
                "shandy.orchestrator.discovery._write_skills_to_claude_dir", new_callable=AsyncMock
            ),
            patch(
                "shandy.orchestrator.discovery._build_agent_executor", return_value=mock_executor
            ),
            patch(
                "shandy.orchestrator.discovery._run_report_generation_phase", new_callable=AsyncMock
            ) as mock_report_phase,
            patch(
                "shandy.orchestrator.discovery._persist_final_status",
                new_callable=AsyncMock,
                return_value="cancelled",
            ),
            patch("shandy.orchestrator.discovery.update_job_status", new_callable=AsyncMock),
            patch("shandy.orchestrator.discovery.sync_knowledge_state_to_db"),
            patch("shandy.orchestrator.discovery._append_iteration_artifacts"),
            patch("shandy.orchestrator.discovery._sync_version_metadata_if_available"),
            patch(
                "shandy.orchestrator.discovery._get_job_status",
                new_callable=AsyncMock,
                side_effect=["running", "cancelled"],
                create=True,
            ),
        ):
            result = await run_discovery_async(job_dir)

        assert result["status"] == "cancelled"
        assert mock_executor.run_iteration.await_count == 1
        mock_report_phase.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_discovery_marks_failed_when_iteration_fails(self, tmp_path):
        from shandy.agent.protocol import IterationResult, TokenUsage
        from shandy.orchestrator.discovery import run_discovery_async

        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        from shandy.knowledge_state import KnowledgeState

        ks = KnowledgeState(job_id, "Question?", 3)
        ks.save(job_dir / "knowledge_state.json")

        runtime = {
            "job_id": job_id,
            "research_question": "Question?",
            "max_iterations": 3,
            "use_skills": False,
            "investigation_mode": "autonomous",
            "data_files": [],
        }

        mock_executor = MagicMock()
        mock_executor.total_tokens = TokenUsage()
        mock_executor.shutdown = AsyncMock()
        mock_executor.run_iteration = AsyncMock(
            side_effect=[
                IterationResult(
                    success=True,
                    output="iteration 1 complete",
                    tool_calls=0,
                    transcript=[],
                ),
                IterationResult(
                    success=False,
                    output="",
                    tool_calls=0,
                    transcript=[],
                    error="iteration 2 exploded",
                ),
            ]
        )

        mock_provider = MagicMock()

        with (
            patch(
                "shandy.orchestrator.discovery._load_runtime_context",
                new_callable=AsyncMock,
                return_value=runtime,
            ),
            patch("shandy.orchestrator.discovery.get_provider", return_value=mock_provider),
            patch(
                "shandy.orchestrator.discovery._write_skills_to_claude_dir", new_callable=AsyncMock
            ),
            patch(
                "shandy.orchestrator.discovery._build_agent_executor", return_value=mock_executor
            ),
            patch(
                "shandy.orchestrator.discovery._run_report_generation_phase", new_callable=AsyncMock
            ) as mock_report_phase,
            patch(
                "shandy.orchestrator.discovery._persist_final_status",
                new_callable=AsyncMock,
                return_value="failed",
            ),
            patch("shandy.orchestrator.discovery.update_job_status", new_callable=AsyncMock),
            patch("shandy.orchestrator.discovery.sync_knowledge_state_to_db"),
            patch("shandy.orchestrator.discovery._append_iteration_artifacts"),
            patch("shandy.orchestrator.discovery._sync_version_metadata_if_available"),
            patch(
                "shandy.orchestrator.discovery._get_job_status",
                new_callable=AsyncMock,
                return_value="running",
                create=True,
            ),
        ):
            result = await run_discovery_async(job_dir)

        assert result["status"] == "failed"
        mock_report_phase.assert_not_awaited()


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


# ─── _write_skills_to_claude_dir ──────────────────────────────────────


class TestWriteSkillsToClaudeDir:
    """Tests for _write_skills_to_claude_dir."""

    def _make_skill(self, *, name, category, slug, description=None, content="Skill content."):
        skill = MagicMock()
        skill.name = name
        skill.category = category
        skill.slug = slug
        skill.description = description
        skill.content = content
        return skill

    @pytest.mark.asyncio
    async def test_writes_skill_files(self, tmp_path):
        from shandy.orchestrator.discovery import _write_skills_to_claude_dir

        skill = self._make_skill(
            name="Hypothesis Generation",
            category="analysis",
            slug="hypothesis-generation",
            description="How to form hypotheses",
            content="Step 1: ...\nStep 2: ...",
        )

        with (
            patch("shandy.orchestrator.discovery.AsyncSessionLocal") as mock_session_cls,
            patch(
                "shandy.orchestrator.discovery.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = [skill]
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await _write_skills_to_claude_dir(tmp_path, use_skills=True)

        skills_dir = tmp_path / ".claude" / "skills"
        assert skills_dir.is_dir()
        md_file = skills_dir / "analysis--hypothesis-generation.md"
        assert md_file.exists()
        content = md_file.read_text(encoding="utf-8")
        assert "# Hypothesis Generation" in content
        assert "*Category: analysis*" in content
        assert "How to form hypotheses" in content
        assert "Step 1:" in content

    @pytest.mark.asyncio
    async def test_no_skills_does_not_create_skills_dir(self, tmp_path):
        from shandy.orchestrator.discovery import _write_skills_to_claude_dir

        with (
            patch("shandy.orchestrator.discovery.AsyncSessionLocal") as mock_session_cls,
            patch(
                "shandy.orchestrator.discovery.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = []
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await _write_skills_to_claude_dir(tmp_path, use_skills=True)

        # .claude/ dir and CLAUDE.md are always written; skills/ subdir is not
        assert (tmp_path / ".claude" / "CLAUDE.md").exists()
        assert not (tmp_path / ".claude" / "skills").exists()

    @pytest.mark.asyncio
    async def test_use_skills_false_still_writes_claude_md(self, tmp_path):
        from shandy.orchestrator.discovery import _write_skills_to_claude_dir

        with patch(
            "shandy.orchestrator.discovery.get_enabled_skills", new_callable=AsyncMock
        ) as mock_get_skills:
            await _write_skills_to_claude_dir(tmp_path, use_skills=False)
            mock_get_skills.assert_not_called()

        # CLAUDE.md is written even when skills are disabled
        assert (tmp_path / ".claude" / "CLAUDE.md").exists()
        assert not (tmp_path / ".claude" / "skills").exists()

    @pytest.mark.asyncio
    async def test_skill_without_description(self, tmp_path):
        from shandy.orchestrator.discovery import _write_skills_to_claude_dir

        skill = self._make_skill(
            name="Stopping Criteria",
            category="workflow",
            slug="stopping-criteria",
            description=None,
            content="Stop when done.",
        )

        with (
            patch("shandy.orchestrator.discovery.AsyncSessionLocal") as mock_session_cls,
            patch(
                "shandy.orchestrator.discovery.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = [skill]
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await _write_skills_to_claude_dir(tmp_path, use_skills=True)

        md_file = tmp_path / ".claude" / "skills" / "workflow--stopping-criteria.md"
        assert md_file.exists()
        content = md_file.read_text(encoding="utf-8")
        assert "# Stopping Criteria" in content
        assert "Stop when done." in content

    @pytest.mark.asyncio
    async def test_always_writes_chat_claude_md(self, tmp_path):
        from shandy.orchestrator.discovery import _write_skills_to_claude_dir

        with (
            patch("shandy.orchestrator.discovery.AsyncSessionLocal") as mock_session_cls,
            patch(
                "shandy.orchestrator.discovery.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = []
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await _write_skills_to_claude_dir(tmp_path, use_skills=True)

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text(encoding="utf-8")
        assert "SHANDY Job Chat Assistant" in content
        assert "knowledge_state.json" in content

    @pytest.mark.asyncio
    async def test_writes_multiple_skill_files(self, tmp_path):
        from shandy.orchestrator.discovery import _write_skills_to_claude_dir

        skills = [
            self._make_skill(name="Skill A", category="cat1", slug="skill-a", content="Content A"),
            self._make_skill(name="Skill B", category="cat2", slug="skill-b", content="Content B"),
        ]

        with (
            patch("shandy.orchestrator.discovery.AsyncSessionLocal") as mock_session_cls,
            patch(
                "shandy.orchestrator.discovery.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = skills
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await _write_skills_to_claude_dir(tmp_path, use_skills=True)

        skills_dir = tmp_path / ".claude" / "skills"
        assert len(list(skills_dir.glob("*.md"))) == 2
        assert (skills_dir / "cat1--skill-a.md").exists()
        assert (skills_dir / "cat2--skill-b.md").exists()


# ─── create_job ───────────────────────────────────────────────────────


class TestCreateJob:
    """Tests for job creation."""

    def test_creates_job_directory(self, tmp_path):
        from shandy.orchestrator import create_job

        job_id = str(uuid4())
        data_file = tmp_path / "test.csv"
        data_file.write_text("a,b\n1,2\n")

        with (
            patch("shandy.orchestrator.setup._persist_data_files_to_db"),
            patch("shandy.orchestrator.setup.sync_knowledge_state_to_db"),
        ):
            job_dir = create_job(
                job_id=job_id,
                research_question="Why?",
                data_files=[data_file],
                max_iterations=5,
                jobs_dir=tmp_path,
            )

        assert job_dir.exists()
        assert not (job_dir / "config.json").exists()
        assert (job_dir / "knowledge_state.json").exists()
        assert (job_dir / "data").is_dir()
        assert (job_dir / "provenance").is_dir()

    def test_knowledge_state_contents(self, tmp_path):
        from shandy.orchestrator import create_job

        job_id = str(uuid4())
        data_file = tmp_path / "test.csv"
        data_file.write_text("a,b\n1,2\n")

        with (
            patch("shandy.orchestrator.setup._persist_data_files_to_db"),
            patch("shandy.orchestrator.setup.sync_knowledge_state_to_db"),
        ):
            job_dir = create_job(
                job_id=job_id,
                research_question="What is X?",
                data_files=[data_file],
                max_iterations=15,
                use_skills=False,
                jobs_dir=tmp_path,
            )

        with open(job_dir / "knowledge_state.json", encoding="utf-8") as f:
            ks = json.load(f)

        assert ks["config"]["job_id"] == job_id
        assert ks["config"]["research_question"] == "What is X?"
        assert ks["config"]["max_iterations"] == 15
        assert ks["config"]["use_skills"] is False

    def test_copies_data_file(self, tmp_path):
        from shandy.orchestrator import create_job

        job_id = str(uuid4())
        data_file = tmp_path / "input_data.csv"
        data_file.write_text("x,y\n1,2\n3,4\n")

        with (
            patch("shandy.orchestrator.setup._persist_data_files_to_db"),
            patch("shandy.orchestrator.setup.sync_knowledge_state_to_db"),
        ):
            job_dir = create_job(
                job_id=job_id,
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

        with (
            patch("shandy.orchestrator.setup._persist_data_files_to_db"),
            patch("shandy.orchestrator.setup.sync_knowledge_state_to_db"),
        ):
            job_dir = create_job(
                job_id=str(uuid4()),
                research_question="Literature only?",
                data_files=[],
                max_iterations=5,
                jobs_dir=tmp_path,
            )

        with open(job_dir / "knowledge_state.json", encoding="utf-8") as f:
            ks = json.load(f)
        assert ks["data_summary"]["files"] == []
        assert ks["data_summary"]["file_type"] == "none"


# ─── build_report_prompt ─────────────────────────────────────────────


class TestBuildReportPrompt:
    """Tests for report prompt construction."""

    def test_uses_concise_outline(self):
        from shandy.knowledge_state import KnowledgeState
        from shandy.orchestrator.iteration import build_report_prompt

        ks = KnowledgeState("j1", "What causes X?", 10)

        # Add 5 findings
        for i in range(5):
            ks.add_finding(f"Finding {i + 1}", f"evidence-{i + 1}")

        # Add iteration summaries
        ks.add_iteration_summary(1, "Explored data", strapline="Data exploration")
        ks.add_iteration_summary(2, "Tested hypothesis A", strapline="Hypothesis A test")

        prompt = build_report_prompt("What causes X?", ks)

        # All 5 finding TITLES should appear (outline omits evidence strings)
        for i in range(5):
            assert f"Finding {i + 1}" in prompt

        # Iteration straplines should appear
        assert "Data exploration" in prompt
        assert "Hypothesis A test" in prompt
        assert "Investigation Timeline" in prompt

        # Must instruct agent to read knowledge_state.json for full details
        assert "knowledge_state.json" in prompt

        # Standard report instructions should still be present
        assert "Executive Summary" in prompt
        assert "set_consensus_answer" in prompt


# ─── _save_transcript ─────────────────────────────────────────────────


class TestSaveTranscript:
    """Tests for _save_transcript()."""

    def test_writes_json_list(self, tmp_path):
        from shandy.orchestrator.discovery import _save_transcript

        path = tmp_path / "transcript.json"
        transcript = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        _save_transcript(path, transcript)

        import json

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["role"] == "user"


# ─── _append_log ──────────────────────────────────────────────────────


class TestAppendLog:
    """Tests for _append_log()."""

    def test_creates_file_in_write_mode(self, tmp_path):
        from shandy.orchestrator.discovery import _append_log

        log_file = tmp_path / "log.txt"
        _append_log(log_file, 1, "prompt1", "output1", 5, write=True)

        content = log_file.read_text(encoding="utf-8")
        assert "Iteration 1" in content
        assert "prompt1" in content
        assert "output1" in content
        assert "Tool calls: 5" in content

    def test_appends_to_existing_file(self, tmp_path):
        from shandy.orchestrator.discovery import _append_log

        log_file = tmp_path / "log.txt"
        _append_log(log_file, 1, "p1", "o1", 3, write=True)
        _append_log(log_file, 2, "p2", "o2", 7, write=False)

        content = log_file.read_text(encoding="utf-8")
        assert "Iteration 1" in content
        assert "Iteration 2" in content


# ─── _write_chat_claude_md ────────────────────────────────────────────


class TestWriteChatClaudeMd:
    """Tests for _write_chat_claude_md()."""

    def test_writes_chat_claude_md(self, tmp_path):
        from shandy.orchestrator.discovery import _write_chat_claude_md

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # Source is relative to discovery.py: ../../../../CHAT_CLAUDE.md
        # We mock it by patching Path to control the source
        chat_src = tmp_path / "CHAT_CLAUDE.md"
        chat_src.write_text("# Chat Claude\nInstructions here", encoding="utf-8")

        with patch("shandy.orchestrator.discovery.Path") as mock_path_cls:
            # Make Path(__file__) chain return our test source
            mock_file_path = MagicMock()
            mock_file_path.parent.parent.parent.parent.__truediv__ = lambda _self, _name: chat_src
            mock_path_cls.return_value = mock_file_path

            # But also make the real Path work for dest
            from pathlib import Path as RealPath

            _write_chat_claude_md(RealPath(claude_dir))

        dest = claude_dir / "CLAUDE.md"
        assert dest.exists()
        content = dest.read_text(encoding="utf-8")
        assert "Chat Claude" in content

    def test_missing_source_no_crash(self, tmp_path):
        from shandy.orchestrator.discovery import _write_chat_claude_md

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # Source file doesn't exist — should log warning but not crash
        _write_chat_claude_md(claude_dir)
        # No CLAUDE.md written, but no exception
        # (the real file may or may not exist depending on working directory)


# ─── build_initial_prompt ──────────────────────────────────────────────


class TestBuildInitialPrompt:
    """Tests for build_initial_prompt()."""

    def test_with_data_files(self):
        from shandy.knowledge_state import KnowledgeState
        from shandy.orchestrator.iteration import build_initial_prompt

        ks = KnowledgeState("j1", "Why X?", 10)
        ks.set_data_summary({"columns": ["a", "b"], "n_samples": 100, "files": ["data.csv"]})

        prompt = build_initial_prompt("Why X?", 10, ["data.csv"], ks)
        assert "Why X?" in prompt
        assert "data.csv" in prompt
        assert "10 iterations" in prompt

    def test_no_data_files(self):
        from shandy.knowledge_state import KnowledgeState
        from shandy.orchestrator.iteration import build_initial_prompt

        ks = KnowledgeState("j1", "Lit only?", 5)

        prompt = build_initial_prompt("Lit only?", 5, [], ks)
        assert "No data files" in prompt
        assert "literature search" in prompt


# ─── build_iteration_prompt ────────────────────────────────────────────


class TestBuildIterationPrompt:
    """Tests for build_iteration_prompt()."""

    def test_with_feedback(self):
        from shandy.knowledge_state import KnowledgeState
        from shandy.orchestrator.iteration import build_iteration_prompt

        ks = KnowledgeState("j1", "Q?", 10)
        prompt = build_iteration_prompt(3, 10, ks, pending_feedback="Focus on X")
        assert "Scientist Feedback" in prompt
        assert "Focus on X" in prompt

    def test_no_feedback(self):
        from shandy.knowledge_state import KnowledgeState
        from shandy.orchestrator.iteration import build_iteration_prompt

        ks = KnowledgeState("j1", "Q?", 10)
        prompt = build_iteration_prompt(2, 10, ks, pending_feedback=None)
        assert "Scientist Feedback" not in prompt
        assert "Iteration 2/10" in prompt

"""Tests for orchestrator helper functions.

Only tests pure/helper functions that don't spawn subprocesses or require
the full agent loop. The run_discovery integration is too heavyweight for
unit testing.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from openscientist.agent.base import TurnOutcome
from openscientist.orchestrator import (
    get_version_metadata,
    increment_ks_iteration,
    update_job_status,
)

# ─── get_version_metadata ─────────────────────────────────────────────


class TestGetVersionMetadata:
    """Tests for version metadata collection."""

    @patch.dict(
        os.environ,
        {
            "OPENSCIENTIST_COMMIT": "abc123def456",
            "OPENSCIENTIST_BUILD_TIME": "2026-02-01T00:00:00",
        },
    )
    def test_from_env_vars(self):
        info = get_version_metadata()
        assert info["openscientist_commit"] == "abc123def456"
        assert info["openscientist_build_time"] == "2026-02-01T00:00:00"

    @patch.dict(os.environ, {"OPENSCIENTIST_COMMIT": "unknown"}, clear=False)
    @patch("openscientist.version._commit", None)
    @patch("subprocess.run")
    def test_falls_back_to_git(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="abcdef123456789\n")
        # Remove build time so it doesn't appear
        with patch.dict(os.environ, {"OPENSCIENTIST_BUILD_TIME": "unknown"}):
            info = get_version_metadata()
        assert info.get("openscientist_commit", "").startswith("abcdef12")

    @patch.dict(os.environ, {}, clear=True)
    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("openscientist.orchestrator.discovery.Path")
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
        job.research_question = "Test job"

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        with patch("openscientist.orchestrator.iteration.AsyncSessionLocal") as mock_session_cls:
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

        owner_id = uuid4()
        job = MagicMock()
        job.status = "running"
        job.owner_id = owner_id
        job.short_title = "Short title"
        job.research_question = "Long title"
        job.current_iteration = 4

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        user_row = MagicMock()
        user_row.ntfy_enabled = True
        user_row.ntfy_topic = "topic-123"
        user_result = MagicMock()
        user_result.first.return_value = user_row

        with (
            patch("openscientist.orchestrator.iteration.AsyncSessionLocal") as mock_session_cls,
            patch(
                "openscientist.orchestrator.iteration.notify_job_status_change",
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
        job.research_question = "Title"

        job_result = MagicMock()
        job_result.scalar_one_or_none.return_value = job

        user_row = MagicMock()
        user_row.ntfy_enabled = True
        user_row.ntfy_topic = "topic-123"
        user_result = MagicMock()
        user_result.first.return_value = user_row

        with (
            patch("openscientist.orchestrator.iteration.AsyncSessionLocal") as mock_session_cls,
            patch(
                "openscientist.orchestrator.iteration.notify_job_status_change",
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
        from openscientist.orchestrator.discovery import _wait_for_coinvestigate_feedback

        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        wait_outcome = {
            "outcome": "cancelled",
            "feedback_text": None,
        }

        with (
            patch(
                "openscientist.orchestrator.discovery.update_job_status", new_callable=AsyncMock
            ) as mock_update,
            patch(
                "openscientist.orchestrator.discovery.wait_for_feedback_or_timeout",
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
        from openscientist.agent.base import IterationResult, TokenUsage
        from openscientist.orchestrator.discovery import run_discovery_async

        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        from openscientist.knowledge_state import KnowledgeState

        ks = KnowledgeState(job_id, "Question?", 3)

        runtime = {
            "job_id": job_id,
            "research_question": "Question?",
            "max_iterations": 3,
            "use_hypotheses": False,
            "investigation_mode": "autonomous",
            "data_files": [],
        }

        mock_executor = MagicMock()
        mock_executor.total_tokens = TokenUsage()
        mock_executor.shutdown = AsyncMock()
        mock_executor.prepare_job_workspace = AsyncMock()
        mock_executor.warm_model_profile = AsyncMock()
        mock_executor.run_iteration = AsyncMock(
            side_effect=[
                IterationResult(
                    outcome=TurnOutcome.COMPLETED,
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
                "openscientist.orchestrator.discovery._load_runtime_context",
                new_callable=AsyncMock,
                return_value=runtime,
            ),
            patch("openscientist.orchestrator.discovery.get_provider", return_value=mock_provider),
            patch(
                "openscientist.orchestrator.discovery._build_agent_executor",
                return_value=mock_executor,
            ),
            patch(
                "openscientist.orchestrator.discovery._run_report_generation_phase",
                new_callable=AsyncMock,
            ) as mock_report_phase,
            patch(
                "openscientist.orchestrator.discovery._persist_final_status",
                new_callable=AsyncMock,
                return_value="cancelled",
            ),
            patch("openscientist.orchestrator.discovery.update_job_status", new_callable=AsyncMock),
            patch("openscientist.orchestrator.discovery._append_iteration_artifacts"),
            patch("openscientist.orchestrator.discovery._sync_version_metadata_if_available"),
            patch(
                "openscientist.orchestrator.discovery.KnowledgeState.load_from_database_sync",
                return_value=ks,
            ),
            patch("openscientist.orchestrator.discovery.increment_ks_iteration"),
            patch(
                "openscientist.orchestrator.discovery._get_job_status",
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
        from openscientist.agent.base import IterationResult, TokenUsage
        from openscientist.orchestrator.discovery import run_discovery_async

        job_id = str(uuid4())
        job_dir = tmp_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        from openscientist.knowledge_state import KnowledgeState

        ks = KnowledgeState(job_id, "Question?", 3)

        runtime = {
            "job_id": job_id,
            "research_question": "Question?",
            "max_iterations": 3,
            "use_hypotheses": False,
            "investigation_mode": "autonomous",
            "data_files": [],
        }

        mock_executor = MagicMock()
        mock_executor.total_tokens = TokenUsage()
        mock_executor.shutdown = AsyncMock()
        mock_executor.prepare_job_workspace = AsyncMock()
        mock_executor.warm_model_profile = AsyncMock()
        mock_executor.run_iteration = AsyncMock(
            side_effect=[
                IterationResult(
                    outcome=TurnOutcome.COMPLETED,
                    output="iteration 1 complete",
                    tool_calls=0,
                    transcript=[],
                ),
                IterationResult(
                    outcome=TurnOutcome.FAILED,
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
                "openscientist.orchestrator.discovery._load_runtime_context",
                new_callable=AsyncMock,
                return_value=runtime,
            ),
            patch("openscientist.orchestrator.discovery.get_provider", return_value=mock_provider),
            patch(
                "openscientist.orchestrator.discovery._build_agent_executor",
                return_value=mock_executor,
            ),
            patch(
                "openscientist.orchestrator.discovery._run_report_generation_phase",
                new_callable=AsyncMock,
            ) as mock_report_phase,
            patch(
                "openscientist.orchestrator.discovery._persist_final_status",
                new_callable=AsyncMock,
                return_value="failed",
            ),
            patch("openscientist.orchestrator.discovery.update_job_status", new_callable=AsyncMock),
            patch("openscientist.orchestrator.discovery._append_iteration_artifacts"),
            patch("openscientist.orchestrator.discovery._sync_version_metadata_if_available"),
            patch(
                "openscientist.orchestrator.discovery.KnowledgeState.load_from_database_sync",
                return_value=ks,
            ),
            patch("openscientist.orchestrator.discovery.increment_ks_iteration"),
            patch(
                "openscientist.orchestrator.discovery._get_job_status",
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

    def test_increments_iteration(self):
        from openscientist.knowledge_state import KnowledgeState

        ks = KnowledgeState("job1", "Q?", 10)
        ks.data["iteration"] = 3

        with (
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.load_from_database_sync",
                return_value=ks,
            ),
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.save_to_database_sync",
            ) as mock_save,
        ):
            increment_ks_iteration("job1")

        assert ks.data["iteration"] == 4
        mock_save.assert_called_once_with("job1")

    def test_preserves_other_fields(self):
        from openscientist.knowledge_state import KnowledgeState

        ks = KnowledgeState("job1", "Q?", 10)
        ks.data["iteration"] = 1
        ks.data["findings"] = [{"id": "F001"}]
        ks.data["hypotheses"] = []

        with (
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.load_from_database_sync",
                return_value=ks,
            ),
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.save_to_database_sync",
            ),
        ):
            increment_ks_iteration("job1")

        assert ks.data["iteration"] == 2
        assert len(ks.data["findings"]) == 1
        assert ks.data["hypotheses"] == []


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
        from openscientist.agent.skills import write_skills_to_claude_dir

        skill = self._make_skill(
            name="Hypothesis Generation",
            category="analysis",
            slug="hypothesis-generation",
            description="How to form hypotheses",
            content="Step 1: ...\nStep 2: ...",
        )

        with (
            patch("openscientist.agent.skills.AsyncSessionLocal") as mock_session_cls,
            patch(
                "openscientist.agent.skills.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = [skill]
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await write_skills_to_claude_dir(tmp_path)

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
        from openscientist.agent.skills import write_skills_to_claude_dir

        with (
            patch("openscientist.agent.skills.AsyncSessionLocal") as mock_session_cls,
            patch(
                "openscientist.agent.skills.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = []
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await write_skills_to_claude_dir(tmp_path)

        # .claude/ dir and CLAUDE.md are always written; skills/ subdir is not
        assert (tmp_path / ".claude" / "CLAUDE.md").exists()
        assert not (tmp_path / ".claude" / "skills").exists()

    @pytest.mark.asyncio
    async def test_skill_without_description(self, tmp_path):
        from openscientist.agent.skills import write_skills_to_claude_dir

        skill = self._make_skill(
            name="Stopping Criteria",
            category="workflow",
            slug="stopping-criteria",
            description=None,
            content="Stop when done.",
        )

        with (
            patch("openscientist.agent.skills.AsyncSessionLocal") as mock_session_cls,
            patch(
                "openscientist.agent.skills.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = [skill]
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await write_skills_to_claude_dir(tmp_path)

        md_file = tmp_path / ".claude" / "skills" / "workflow--stopping-criteria.md"
        assert md_file.exists()
        content = md_file.read_text(encoding="utf-8")
        assert "# Stopping Criteria" in content
        assert "Stop when done." in content

    @pytest.mark.asyncio
    async def test_always_writes_job_claude_md(self, tmp_path):
        from openscientist.agent.skills import write_skills_to_claude_dir

        with (
            patch("openscientist.agent.skills.AsyncSessionLocal") as mock_session_cls,
            patch(
                "openscientist.agent.skills.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = []
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await write_skills_to_claude_dir(tmp_path)

        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text(encoding="utf-8")
        assert "OpenScientist: Scientific Hypothesis Agent for Novel Discovery" in content
        assert "execute_code" in content

    @pytest.mark.asyncio
    async def test_writes_multiple_skill_files(self, tmp_path):
        from openscientist.agent.skills import write_skills_to_claude_dir

        skills = [
            self._make_skill(name="Skill A", category="cat1", slug="skill-a", content="Content A"),
            self._make_skill(name="Skill B", category="cat2", slug="skill-b", content="Content B"),
        ]

        with (
            patch("openscientist.agent.skills.AsyncSessionLocal") as mock_session_cls,
            patch(
                "openscientist.agent.skills.get_enabled_skills", new_callable=AsyncMock
            ) as mock_get_skills,
        ):
            mock_get_skills.return_value = skills
            mock_cm = AsyncMock()
            mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
            mock_cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_cm

            await write_skills_to_claude_dir(tmp_path)

        skills_dir = tmp_path / ".claude" / "skills"
        assert len(list(skills_dir.glob("*.md"))) == 2
        assert (skills_dir / "cat1--skill-a.md").exists()
        assert (skills_dir / "cat2--skill-b.md").exists()


# ─── create_job ───────────────────────────────────────────────────────


class TestCreateJob:
    """Tests for job creation."""

    def test_creates_job_directory(self, tmp_path):
        from openscientist.orchestrator import create_job

        job_id = str(uuid4())
        data_file = tmp_path / "test.csv"
        data_file.write_text("a,b\n1,2\n")

        with (
            patch("openscientist.orchestrator.setup._persist_data_files_to_db"),
            patch("openscientist.orchestrator.setup.KnowledgeState.save_to_database_sync"),
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
        assert not (job_dir / "knowledge_state.json").exists()
        assert (job_dir / "data").is_dir()
        assert (job_dir / "provenance").is_dir()

    def test_knowledge_state_contents(self, tmp_path):
        from openscientist.orchestrator import create_job

        job_id = str(uuid4())
        data_file = tmp_path / "test.csv"
        data_file.write_text("a,b\n1,2\n")

        captured: dict[str, Any] = {}

        def _capture_save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured["data"] = self.to_dict()

        with (
            patch("openscientist.orchestrator.setup._persist_data_files_to_db"),
            patch(
                "openscientist.orchestrator.setup.KnowledgeState.save_to_database_sync",
                autospec=True,
                side_effect=_capture_save,
            ),
        ):
            create_job(
                job_id=job_id,
                research_question="What is X?",
                data_files=[data_file],
                max_iterations=15,
                jobs_dir=tmp_path,
            )

        ks = captured["data"]

        assert ks["config"]["job_id"] == job_id
        assert ks["config"]["research_question"] == "What is X?"
        assert ks["config"]["max_iterations"] == 15

    def test_copies_data_file(self, tmp_path):
        from openscientist.orchestrator import create_job

        job_id = str(uuid4())
        data_file = tmp_path / "input_data.csv"
        data_file.write_text("x,y\n1,2\n3,4\n")

        with (
            patch("openscientist.orchestrator.setup._persist_data_files_to_db"),
            patch("openscientist.orchestrator.setup.KnowledgeState.save_to_database_sync"),
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
        from openscientist.orchestrator import create_job

        captured: dict[str, Any] = {}

        def _capture_save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured["data"] = self.to_dict()

        with (
            patch("openscientist.orchestrator.setup._persist_data_files_to_db"),
            patch(
                "openscientist.orchestrator.setup.KnowledgeState.save_to_database_sync",
                autospec=True,
                side_effect=_capture_save,
            ),
        ):
            create_job(
                job_id=str(uuid4()),
                research_question="Literature only?",
                data_files=[],
                max_iterations=5,
                jobs_dir=tmp_path,
            )

        ks = captured["data"]
        assert ks["data_summary"]["files"] == []
        assert ks["data_summary"]["file_type"] == "none"


# ─── build_report_prompt ─────────────────────────────────────────────


class TestBuildReportPrompt:
    """Tests for report prompt construction."""

    def test_uses_concise_outline(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_report_prompt

        ks = KnowledgeState("j1", "What causes X?", 10)

        # Add 5 findings
        for i in range(5):
            ks.add_finding(f"Finding {i + 1}", f"evidence-{i + 1}")

        # Add iteration summaries
        ks.add_iteration_summary(1, "Explored data", strapline="Data exploration")
        ks.add_iteration_summary(2, "Tested hypothesis A", strapline="Hypothesis A test")

        prompt = build_report_prompt("What causes X?", ks, context_window_tokens=131072)

        # All 5 finding TITLES should appear (outline omits evidence strings)
        for i in range(5):
            assert f"Finding {i + 1}" in prompt

        # Iteration straplines should appear
        assert "Data exploration" in prompt
        assert "Hypothesis A test" in prompt
        assert "Investigation Timeline" in prompt

        # Prompt should no longer instruct file-based context loading.
        assert "knowledge_state.json" not in prompt

        # Standard report instructions should still be present
        assert "Summary" in prompt
        # The consensus is a separate turn now, not part of the report prompt.
        assert "set_consensus_answer" not in prompt

    def test_report_prompt_includes_abstracts(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_report_prompt

        ks = KnowledgeState("j1", "What causes X?", 10)
        ks.add_literature(
            "12345678", "A Relevant Paper", "This study demonstrates that X causes Y."
        )
        prompt = build_report_prompt("What causes X?", ks, context_window_tokens=131072)

        # Abstract should flow through to the report prompt for citation grounding
        assert "This study demonstrates that X causes Y." in prompt
        assert "PMID: 12345678" in prompt

    def test_report_prompt_has_citation_integrity_instruction(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_report_prompt

        ks = KnowledgeState("j1", "What causes X?", 10)
        prompt = build_report_prompt("What causes X?", ks, context_window_tokens=131072)
        assert "do not infer paper content from titles alone" in prompt

    def test_report_prompt_names_the_file_write_tool(self):
        # The model must be told exactly which tool to call, not "your
        # file-writing tool", so it invokes it instead of printing the call.
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import (
            build_report_prompt,
            build_report_retry_prompt,
        )

        ks = KnowledgeState("j1", "What causes X?", 10)
        prompt = build_report_prompt(
            "What causes X?", ks, file_write_tool="apply_patch", context_window_tokens=131072
        )
        assert "`apply_patch`" in prompt
        assert "your file-writing tool" not in prompt
        retry = build_report_retry_prompt(
            "What causes X?", ks, file_write_tool="apply_patch", context_window_tokens=131072
        )
        assert "`apply_patch`" in retry

    def test_report_prompt_has_citation_snippet_instruction(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_report_prompt

        ks = KnowledgeState("j1", "What causes X?", 10)
        prompt = build_report_prompt("What causes X?", ks, context_window_tokens=131072)
        assert "use the provided citation snippets" in prompt

    def test_report_prompt_includes_finding_citations(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_report_prompt

        ks = KnowledgeState("j1", "What causes X?", 10)
        ks.add_literature("99999999", "A Study", "significant correlation was found")
        ks.add_finding(
            "X correlates with Y",
            "r=0.85, p<0.001",
            citations=[
                {
                    "pmid": "99999999",
                    "snippet": "significant correlation was found",
                    "explanation": "Direct evidence",
                }
            ],
        )
        prompt = build_report_prompt("What causes X?", ks, context_window_tokens=131072)
        assert "PMID:99999999" in prompt
        assert "significant correlation was found" in prompt

    def test_report_prompt_includes_job_description(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_report_prompt

        ks = KnowledgeState("j1", "What causes X?", 10)
        prompt = build_report_prompt(
            "What causes X?",
            ks,
            description="Emphasize validated clinical endpoints.",
            context_window_tokens=131072,
        )
        assert "Additional job context" in prompt
        assert "Emphasize validated clinical endpoints." in prompt


# ─── _save_transcript ─────────────────────────────────────────────────


class TestSaveTranscript:
    """Tests for _save_transcript()."""

    def test_writes_json_list(self, tmp_path):
        from openscientist.orchestrator.discovery import _save_transcript
        from openscientist.transcript import AssistantText, UserPrompt, load_transcript

        path = tmp_path / "transcript.json"
        transcript = [UserPrompt(text="hello"), AssistantText(text="hi")]
        _save_transcript(path, transcript)

        loaded = load_transcript(path)
        assert loaded == transcript


# ─── _append_log ──────────────────────────────────────────────────────


class TestAppendLog:
    """Tests for _append_log()."""

    def test_creates_file_in_write_mode(self, tmp_path):
        from openscientist.orchestrator.discovery import _append_log

        log_file = tmp_path / "log.txt"
        _append_log(log_file, 1, "prompt1", "output1", 5, write=True)

        content = log_file.read_text(encoding="utf-8")
        assert "Iteration 1" in content
        assert "prompt1" in content
        assert "output1" in content
        assert "Tool calls: 5" in content

    def test_appends_to_existing_file(self, tmp_path):
        from openscientist.orchestrator.discovery import _append_log

        log_file = tmp_path / "log.txt"
        _append_log(log_file, 1, "p1", "o1", 3, write=True)
        _append_log(log_file, 2, "p2", "o2", 7, write=False)

        content = log_file.read_text(encoding="utf-8")
        assert "Iteration 1" in content
        assert "Iteration 2" in content


# ─── packaged chat template ───────────────────────────────────────────


class TestChatTemplate:
    """The packaged CHAT_CLAUDE.md must match the repo copy so the in-image
    template stays in sync with the source of truth. (Writing the chat context
    is the agent's job, exercised in tests/test_job_chat.py.)"""

    def test_packaged_template_matches_repo_copy(self):
        from openscientist.prompts.common import read_chat_template

        repo_copy = (Path(__file__).resolve().parents[1] / "CHAT_CLAUDE.md").read_text(
            encoding="utf-8"
        )
        assert read_chat_template() == repo_copy


# ─── build_initial_prompt ──────────────────────────────────────────────


class TestBuildInitialPrompt:
    """Tests for build_initial_prompt()."""

    def test_with_data_files(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_initial_prompt

        ks = KnowledgeState("j1", "Why X?", 10)
        ks.set_data_summary({"columns": ["a", "b"], "n_samples": 100, "files": ["data.csv"]})

        prompt = build_initial_prompt("Why X?", 10, ["data.csv"], ks)
        assert "Why X?" in prompt
        assert "data.csv" in prompt
        assert "iteration 1 of 10" in prompt

    def test_no_data_files(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_initial_prompt

        ks = KnowledgeState("j1", "Lit only?", 5)

        prompt = build_initial_prompt("Lit only?", 5, [], ks)
        assert "No data files" in prompt
        assert "literature search" in prompt

    def test_includes_job_description(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_initial_prompt

        ks = KnowledgeState("j1", "Why X?", 10)
        prompt = build_initial_prompt(
            "Why X?",
            10,
            [],
            ks,
            description="Prioritize longitudinal cohort evidence.",
        )
        assert "Additional job context" in prompt
        assert "Prioritize longitudinal cohort evidence." in prompt

    def test_frames_set_status_as_not_progress(self):
        # The model used to stall after the "REQUIRED first call set_status"
        # framing. The prompt must now make clear a status is not progress and
        # the iteration is not complete without real analysis.
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_initial_prompt

        ks = KnowledgeState("j1", "Why X?", 10)
        prompt = build_initial_prompt("Why X?", 10, ["data.csv"], ks)
        assert "not progress" in prompt
        assert "not complete until" in prompt
        assert "your very first tool call must be set_status" not in prompt.lower()


# ─── build_iteration_prompt ────────────────────────────────────────────


class TestBuildIterationPrompt:
    """Tests for build_iteration_prompt()."""

    def test_with_feedback(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_iteration_prompt

        ks = KnowledgeState("j1", "Q?", 10)
        prompt = build_iteration_prompt(3, 10, ks, pending_feedback="Focus on X")
        assert "Scientist Feedback" in prompt
        assert "Focus on X" in prompt

    def test_no_feedback(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_iteration_prompt

        ks = KnowledgeState("j1", "Q?", 10)
        prompt = build_iteration_prompt(2, 10, ks, pending_feedback=None)
        assert "Scientist Feedback" not in prompt
        assert "Iteration 2 of 10" in prompt

    def test_includes_job_description(self):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_iteration_prompt

        ks = KnowledgeState("j1", "Q?", 10)
        prompt = build_iteration_prompt(
            2,
            10,
            ks,
            description="Stay focused on the uploaded assay design.",
        )
        assert "Additional job context" in prompt
        assert "Stay focused on the uploaded assay design." in prompt

    def test_forbids_status_only_turns(self):
        # Directly counters the observed "narrate intent then stall" failure:
        # a turn that only sets a status must be called out as a wasted iteration.
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import build_iteration_prompt

        ks = KnowledgeState("j1", "Q?", 10)
        prompt = build_iteration_prompt(3, 10, ks)
        assert "not progress" in prompt
        assert "wastes the iteration" in prompt
        assert "not complete until" in prompt

    def test_does_not_tell_model_to_label_summaries_with_iteration_number(self):
        # Regression: instructing the model to call its summary "Iteration N"
        # made gpt-oss:20b prefix every strapline, which the UI then duplicated
        # as "Iteration N: Iteration N: ...". The number is added by the system.
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import (
            build_initial_prompt,
            build_iteration_prompt,
        )

        ks = KnowledgeState("j1", "Q?", 10)
        for prompt in (
            build_iteration_prompt(3, 10, ks),
            build_initial_prompt("Q?", 10, [], ks),
        ):
            assert "in summaries" not in prompt
            assert "Refer to" not in prompt


class TestReportGenerationPhase:
    """Report and consensus turns, each with bounded retries (the model writes
    each deliverable itself, and the job fails honestly only if attempts run out)."""

    def test_prompts_are_focused(self):
        from openscientist.orchestrator.iteration import (
            build_consensus_prompt,
            build_consensus_retry_prompt,
        )

        assert "set_consensus_answer" in build_consensus_prompt("Does X cause Y?")
        assert "Does X cause Y?" in build_consensus_prompt("Does X cause Y?")
        assert "set_consensus_answer" in build_consensus_retry_prompt("Does X cause Y?")

    def test_report_retry_prompt_is_self_contained(self, tmp_path: Path):
        """The retry must restate the whole task, not just remind the model to
        write a file. A weak model re-anchors on the last instruction, so the
        retry has to carry the findings outline, the required structure, and the
        exact path -- everything the first attempt had -- plus a correction that
        rejects describing the report instead of writing it."""
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import (
            build_report_prompt,
            build_report_retry_prompt,
        )

        ks = KnowledgeState("j", "What causes X?", 3)
        retry = build_report_retry_prompt(
            "What causes X?", ks, job_dir=tmp_path, context_window_tokens=131072
        )
        base = build_report_prompt(
            "What causes X?", ks, job_dir=tmp_path, context_window_tokens=131072
        )

        # The exact write path and the research question both survive.
        report_path = str(tmp_path.resolve() / "final_report.md")
        assert report_path in retry
        assert "What causes X?" in retry
        # The retry embeds the full self-contained spec verbatim ...
        assert base in retry
        # ... behind a correction that names the failure mode (printed/described
        # instead of written) so the model does not repeat it.
        assert retry.index("That output is rejected") < retry.index(base)
        assert "describe" in retry.lower()

    @staticmethod
    def _executor(record: list) -> SimpleNamespace:
        from openscientist.agent.base import IterationResult

        async def fake_run(prompt: str, *, reset_session: bool = False) -> IterationResult:
            record.append((prompt, reset_session))
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
            )

        return SimpleNamespace(
            run_iteration=fake_run,
            file_write_tool="Write",
            model_profile=SimpleNamespace(context_window_tokens=131072),
        )

    @pytest.mark.asyncio
    async def test_report_turn_succeeds_first_attempt(self, tmp_path: Path):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        calls: list[tuple[str, bool]] = []
        with (
            patch.object(discovery, "build_report_prompt", return_value="FULL"),
            patch.object(discovery, "_ensure_report_written", return_value=True),
        ):
            _, ok = await discovery._run_report_turn(
                self._executor(calls),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
                ks,
                None,
            )
        assert ok is True
        assert calls == [("FULL", False)]  # one attempt, continues the session

    @pytest.mark.asyncio
    async def test_report_turn_retries_until_written(self, tmp_path: Path):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        calls: list[tuple[str, bool]] = []
        with (
            patch.object(discovery, "build_report_prompt", return_value="FULL"),
            patch.object(discovery, "build_report_retry_prompt", return_value="RETRY"),
            patch.object(discovery, "_ensure_report_written", side_effect=[False, True]),
        ):
            _, ok = await discovery._run_report_turn(
                self._executor(calls),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
                ks,
                None,
            )
        assert ok is True
        # The full prompt, then a focused retry, both continue the same session.
        assert calls == [("FULL", False), ("RETRY", False)]

    @pytest.mark.asyncio
    async def test_report_turn_gives_up_after_max(self, tmp_path: Path):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        calls: list[tuple[str, bool]] = []
        with (
            patch.object(discovery, "build_report_prompt", return_value="FULL"),
            patch.object(discovery, "build_report_retry_prompt", return_value="RETRY"),
            patch.object(discovery, "_ensure_report_written", return_value=False),
        ):
            _, ok = await discovery._run_report_turn(
                self._executor(calls),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
                ks,
                None,
            )
        assert ok is False
        assert len(calls) == discovery._MAX_REPORT_ATTEMPTS

    @pytest.mark.asyncio
    async def test_consensus_retries_until_recorded(self, tmp_path: Path):
        from openscientist.agent.base import IterationResult
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        calls: list[str] = []

        async def fake_run(prompt: str, *, reset_session: bool = False) -> IterationResult:
            calls.append(prompt)
            if len(calls) == 2:  # model records it on the second attempt
                ks.data["consensus_answer"] = "A"
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
            )

        with patch.object(discovery.KnowledgeState, "load_from_database_sync", return_value=ks):
            await discovery._set_consensus_answer(
                SimpleNamespace(
                    run_iteration=fake_run,
                    file_write_tool="Write",
                    model_profile=SimpleNamespace(context_window_tokens=131072),
                ),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
            )
        assert ks.data["consensus_answer"] == "A"
        assert len(calls) == 2  # first attempt + one retry

    @pytest.mark.asyncio
    async def test_consensus_rejects_stale_baseline(self, tmp_path: Path):
        # On regeneration the KS already holds the prior run's consensus. A turn
        # that does not write a new one must NOT be accepted as fresh, so the
        # model is re-asked until attempts run out (the stale value is never
        # mistaken for this turn's output).
        from openscientist.agent.base import IterationResult
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        ks.data["consensus_answer"] = "STALE from a prior run"
        calls: list[str] = []

        async def fake_run(prompt: str, *, reset_session: bool = False) -> IterationResult:
            calls.append(prompt)  # never writes a new consensus
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
            )

        with patch.object(discovery.KnowledgeState, "load_from_database_sync", return_value=ks):
            await discovery._set_consensus_answer(
                SimpleNamespace(run_iteration=fake_run, file_write_tool="Write"),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
            )
        # Re-asked the full budget; the stale value was never accepted as fresh.
        assert len(calls) == discovery._MAX_CONSENSUS_ATTEMPTS

    @pytest.mark.asyncio
    async def test_consensus_accepts_a_freshly_changed_answer_over_baseline(self, tmp_path: Path):
        from openscientist.agent.base import IterationResult
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        ks.data["consensus_answer"] = "OLD"
        calls: list[str] = []

        async def fake_run(prompt: str, *, reset_session: bool = False) -> IterationResult:
            calls.append(prompt)
            if len(calls) == 2:  # model writes a NEW consensus on the second attempt
                ks.data["consensus_answer"] = "NEW"
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
            )

        with patch.object(discovery.KnowledgeState, "load_from_database_sync", return_value=ks):
            await discovery._set_consensus_answer(
                SimpleNamespace(run_iteration=fake_run, file_write_tool="Write"),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
            )
        assert ks.data["consensus_answer"] == "NEW"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_phase_happy_path(self, tmp_path: Path):
        from openscientist.agent.base import IterationResult
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        ks.data["consensus_answer"] = "A"

        async def fake_run(prompt: str, *, reset_session: bool = False) -> IterationResult:
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
            )

        with (
            patch.object(discovery.KnowledgeState, "load_from_database_sync", return_value=ks),
            patch.object(discovery, "build_report_prompt", return_value="R"),
            patch.object(discovery, "_save_report_transcript"),
            patch.object(discovery, "_ensure_report_written", return_value=True),
            patch.object(discovery, "_try_generate_report_pdf", new=AsyncMock()) as pdf,
        ):
            outcome = await discovery._run_report_generation_phase(
                SimpleNamespace(
                    run_iteration=fake_run,
                    file_write_tool="Write",
                    model_profile=SimpleNamespace(context_window_tokens=131072),
                ),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
            )
        assert outcome.success is True
        pdf.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_phase_fails_when_report_never_written(self, tmp_path: Path):
        from openscientist.agent.base import IterationResult
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        calls: list[str] = []

        async def fake_run(prompt: str, *, reset_session: bool = False) -> IterationResult:
            calls.append(prompt)
            return IterationResult(
                outcome=TurnOutcome.FAILED, output="", tool_calls=0, transcript=[], error="x"
            )

        with (
            patch.object(discovery.KnowledgeState, "load_from_database_sync", return_value=ks),
            patch.object(discovery, "build_report_prompt", return_value="R"),
            patch.object(discovery, "build_report_retry_prompt", return_value="RETRY"),
            patch.object(discovery, "_save_report_transcript"),
            patch.object(discovery, "_ensure_report_written", return_value=False),
        ):
            outcome = await discovery._run_report_generation_phase(
                SimpleNamespace(
                    run_iteration=fake_run,
                    file_write_tool="Write",
                    model_profile=SimpleNamespace(context_window_tokens=131072),
                ),  # type: ignore[arg-type]
                tmp_path,
                "Q?",
            )
        assert outcome.success is False
        # Report attempted _MAX times, so the consensus turn is never reached.
        assert len(calls) == discovery._MAX_REPORT_ATTEMPTS


class TestRegenerateReportAsync:
    """The admin report-only re-run reuses persisted findings and must NOT
    re-run the discovery iterations."""

    @staticmethod
    def _runtime() -> dict[str, Any]:
        return {
            "job_id": "j",
            "research_question": "Q?",
            "description": "ctx",
            "use_hypotheses": False,
            "data_files": [],
            "max_iterations": 3,
            "investigation_mode": "autonomous",
        }

    @pytest.mark.asyncio
    async def test_skips_discovery_loop_and_runs_report(self, tmp_path: Path):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator import discovery

        ks = KnowledgeState("j", "Q?", 3)
        executor = SimpleNamespace()
        report_phase = AsyncMock(return_value=discovery._ReportOutcome(success=True, error=""))

        with (
            patch.object(
                discovery, "_load_runtime_context", new=AsyncMock(return_value=self._runtime())
            ),
            patch.object(
                discovery, "_build_and_prepare_executor", new=AsyncMock(return_value=executor)
            ),
            patch.object(discovery, "_run_primary_discovery_loop", new=AsyncMock()) as loop,
            patch.object(discovery, "_run_report_generation_phase", new=report_phase),
            patch.object(
                discovery, "_persist_final_status", new=AsyncMock(return_value="completed")
            ),
            patch.object(discovery, "_finalize_executor", new=AsyncMock()) as finalize,
            patch.object(discovery.KnowledgeState, "load_from_database_sync", return_value=ks),
        ):
            result = await discovery.regenerate_report_async(tmp_path)

        # The discovery iterations are never re-run, only the report phase runs.
        loop.assert_not_awaited()
        report_phase.assert_awaited_once()
        # The executor is always finalized (cost record + shutdown).
        finalize.assert_awaited_once()
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_failure_marks_job_failed_and_finalizes(self, tmp_path: Path):
        from openscientist.orchestrator import discovery

        executor = SimpleNamespace()

        with (
            patch.object(
                discovery, "_load_runtime_context", new=AsyncMock(return_value=self._runtime())
            ),
            patch.object(
                discovery, "_build_and_prepare_executor", new=AsyncMock(return_value=executor)
            ),
            patch.object(
                discovery,
                "_run_report_generation_phase",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(discovery, "update_job_status", new=AsyncMock()) as update_status,
            patch.object(discovery, "_finalize_executor", new=AsyncMock()) as finalize,
            patch.object(
                discovery.KnowledgeState,
                "load_from_database_sync",
                side_effect=Exception("no ks"),
            ),
        ):
            result = await discovery.regenerate_report_async(tmp_path)

        assert result["status"] == "failed"
        update_status.assert_awaited()  # job marked failed
        finalize.assert_awaited_once()  # executor still cleaned up


class TestEnsureReportWritten:
    """Freshness check: a stale report from a prior run must not be mistaken
    for fresh output (the bug that made report regeneration a silent no-op)."""

    @staticmethod
    def _result():
        from openscientist.agent.base import IterationResult

        return IterationResult(
            outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
        )

    def test_missing_file_is_not_written(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _ensure_report_written

        assert _ensure_report_written(tmp_path / "final_report.md", self._result()) is False

    def test_existing_file_no_baseline_is_written(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _ensure_report_written

        report = tmp_path / "final_report.md"
        report.write_text("content")
        # No baseline (fresh job): existence is sufficient.
        assert _ensure_report_written(report, self._result()) is True

    def test_stale_file_unchanged_since_baseline_is_not_written(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _ensure_report_written

        report = tmp_path / "final_report.md"
        report.write_text("stale report from a previous run")
        baseline = report.stat().st_mtime_ns
        # The model claimed success but never rewrote the file: mtime == baseline.
        assert _ensure_report_written(report, self._result(), baseline_mtime_ns=baseline) is False

    def test_freshly_rewritten_file_is_written(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _ensure_report_written

        report = tmp_path / "final_report.md"
        report.write_text("stale")
        baseline = report.stat().st_mtime_ns - 1_000_000  # report is now strictly newer
        assert _ensure_report_written(report, self._result(), baseline_mtime_ns=baseline) is True

    def test_nested_fresh_report_is_moved_into_place(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _ensure_report_written

        report = tmp_path / "final_report.md"
        nested = tmp_path / "sub" / "final_report.md"
        nested.parent.mkdir()
        nested.write_text("agent nested it here")
        # No stale top-level file and no baseline: the nested file is recovered.
        assert _ensure_report_written(report, self._result()) is True
        assert report.exists() and report.read_text() == "agent nested it here"


class TestReportAbstractBudget:
    """The literature budget must scale with the model's context window."""

    def test_budget_scales_with_context_and_has_a_floor(self):
        from openscientist.orchestrator.iteration import (
            _REPORT_PROMPT_CHARS_PER_TOKEN,
            _REPORT_PROMPT_MIN_ABSTRACT_CHARS,
            _REPORT_PROMPT_RESERVE_TOKENS,
            _report_abstract_budget_chars,
        )

        big = _report_abstract_budget_chars(131072)
        small = _report_abstract_budget_chars(16384)
        assert big > small
        assert big == int((131072 - _REPORT_PROMPT_RESERVE_TOKENS) * _REPORT_PROMPT_CHARS_PER_TOKEN)
        # A context smaller than the reserve clamps to the floor, never negative.
        assert _report_abstract_budget_chars(1000) == _REPORT_PROMPT_MIN_ABSTRACT_CHARS


class TestRunReportTurnIntegration:
    """Exercise _run_report_turn against a real file on disk: freshness check,
    retries, and the file_write_tool passthrough, mocking only the LLM turn."""

    @staticmethod
    def _executor(job_dir: Path, write_on: set[int]) -> SimpleNamespace:
        from openscientist.agent.base import IterationResult

        calls = {"n": 0}

        async def run(prompt: str, *, reset_session: bool = False) -> IterationResult:
            calls["n"] += 1
            if calls["n"] in write_on:
                (job_dir / "final_report.md").write_text(
                    "# Final Report\n\n" + "body " * 60, encoding="utf-8"
                )
            return IterationResult(
                outcome=TurnOutcome.COMPLETED,
                output="printed, not written",
                tool_calls=1,
                transcript=[],
            )

        return SimpleNamespace(
            run_iteration=run,
            file_write_tool="Write",
            calls=calls,
            model_profile=SimpleNamespace(context_window_tokens=131072),
        )

    @pytest.mark.asyncio
    async def test_succeeds_when_model_writes_on_first_attempt(self, tmp_path: Path):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.discovery import _run_report_turn

        ex = self._executor(tmp_path, {1})
        _, ok = await _run_report_turn(ex, tmp_path, "Q?", KnowledgeState("j", "Q?", 3), None)  # type: ignore[arg-type]
        assert ok is True
        assert ex.calls["n"] == 1
        assert (tmp_path / "final_report.md").read_text().startswith("# Final Report")

    @pytest.mark.asyncio
    async def test_recovers_when_model_writes_on_retry(self, tmp_path: Path):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.discovery import _run_report_turn

        ex = self._executor(tmp_path, {2})  # writes only on the second attempt
        _, ok = await _run_report_turn(ex, tmp_path, "Q?", KnowledgeState("j", "Q?", 3), None)  # type: ignore[arg-type]
        assert ok is True
        assert ex.calls["n"] == 2

    @pytest.mark.asyncio
    async def test_fails_when_file_never_written(self, tmp_path: Path):
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.discovery import _MAX_REPORT_ATTEMPTS, _run_report_turn

        ex = self._executor(tmp_path, set())  # never writes
        _, ok = await _run_report_turn(ex, tmp_path, "Q?", KnowledgeState("j", "Q?", 3), None)  # type: ignore[arg-type]
        assert ok is False
        assert ex.calls["n"] == _MAX_REPORT_ATTEMPTS

    @pytest.mark.asyncio
    async def test_stale_report_is_not_accepted(self, tmp_path: Path):
        # A leftover report from a prior run must not pass as this turn's output.
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.discovery import _run_report_turn

        (tmp_path / "final_report.md").write_text("stale prior report", encoding="utf-8")
        ex = self._executor(tmp_path, set())  # model never writes a fresh one
        _, ok = await _run_report_turn(ex, tmp_path, "Q?", KnowledgeState("j", "Q?", 3), None)  # type: ignore[arg-type]
        assert ok is False


class TestTurnOutcomePolicy:
    """The discovery loop interprets a turn's outcome: FAILED aborts the run,
    TIMED_OUT advances but is recorded, COMPLETED proceeds."""

    @staticmethod
    def _result(outcome: TurnOutcome, tool_calls: int = 0):
        from openscientist.agent.base import IterationResult

        return IterationResult(
            outcome=outcome,
            output="",
            tool_calls=tool_calls,
            transcript=[],
            error="boom" if outcome is TurnOutcome.FAILED else "",
        )

    def test_failed_turn_aborts_the_run(self):
        from openscientist.orchestrator.discovery import _check_turn_outcome

        with pytest.raises(RuntimeError, match="Iteration 3 failed"):
            _check_turn_outcome(self._result(TurnOutcome.FAILED), 3)

    def test_timed_out_turn_advances(self):
        from openscientist.orchestrator.discovery import _check_turn_outcome

        # Must NOT raise: a wall-clock timeout advances the loop (work before the
        # cut is persisted), unlike a failure.
        _check_turn_outcome(self._result(TurnOutcome.TIMED_OUT, tool_calls=2), 2)

    def test_completed_turn_advances(self):
        from openscientist.orchestrator.discovery import _check_turn_outcome

        _check_turn_outcome(self._result(TurnOutcome.COMPLETED), 1)

    def test_append_log_records_timeout(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _append_log

        log = tmp_path / "log.txt"
        _append_log(log, 2, "prompt", "output", 0, write=True, timed_out=True)
        assert "Timed out: yes" in log.read_text()

    def test_append_log_omits_timeout_line_when_not_timed_out(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _append_log

        log = tmp_path / "log.txt"
        _append_log(log, 1, "prompt", "output", 1, write=True)
        assert "Timed out" not in log.read_text()

    def test_artifacts_record_timeout_from_outcome(self, tmp_path: Path):
        from openscientist.orchestrator.discovery import _append_iteration_artifacts

        provenance = tmp_path / "prov"
        provenance.mkdir()
        log = tmp_path / "log.txt"
        _append_iteration_artifacts(
            provenance_dir=provenance,
            log_file=log,
            iteration=4,
            prompt="p",
            result=self._result(TurnOutcome.TIMED_OUT),
            overwrite_log=True,
        )
        assert "Timed out: yes" in log.read_text()

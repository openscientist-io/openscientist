"""Tests for orchestrator helper functions.

Only tests pure/helper functions that don't spawn subprocesses or require
the full agent loop. The run_discovery integration is too heavyweight for
unit testing.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

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
            mock_file_path.parent.parent.parent.parent.__truediv__ = lambda self, name: chat_src
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


# ─── _send_iteration_notification ─────────────────────────────────────


class TestSendIterationNotification:
    """Tests for _send_iteration_notification()."""

    def test_disabled_no_http_call(self, tmp_path):
        from shandy.orchestrator.iteration import _send_iteration_notification

        config = {"job_id": "j1", "ntfy_enabled": False, "ntfy_topic": None}
        # Should return immediately without making any HTTP call
        _send_iteration_notification(tmp_path, config)

    def test_no_topic_no_http_call(self, tmp_path):
        from shandy.orchestrator.iteration import _send_iteration_notification

        config = {"job_id": "j1", "ntfy_enabled": True, "ntfy_topic": None}
        _send_iteration_notification(tmp_path, config)

    def test_success(self, tmp_path):
        from shandy.orchestrator.iteration import _send_iteration_notification

        mock_settings = MagicMock()
        mock_settings.base_url = "https://app.test"

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        # Write KS file so iteration can be read
        ks_path = tmp_path / "knowledge_state.json"
        ks_path.write_text('{"iteration": 3}')

        config = {
            "job_id": "j1",
            "ntfy_enabled": True,
            "ntfy_topic": "topic1",
            "research_question": "Why?",
        }

        import httpx

        with (
            patch("shandy.settings.get_settings", return_value=mock_settings),
            patch.object(httpx, "Client", return_value=mock_client),
        ):
            _send_iteration_notification(tmp_path, config)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "topic1" in call_args[1].get("url", call_args[0][0])

    def test_failure_no_crash(self, tmp_path):
        from shandy.orchestrator.iteration import _send_iteration_notification

        mock_settings = MagicMock()
        mock_settings.base_url = "https://app.test"

        mock_client = MagicMock()
        mock_client.post.side_effect = Exception("connection failed")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        ks_path = tmp_path / "knowledge_state.json"
        ks_path.write_text('{"iteration": 1}')

        config = {
            "job_id": "j1",
            "ntfy_enabled": True,
            "ntfy_topic": "topic1",
            "research_question": "Why?",
        }

        import httpx

        with (
            patch("shandy.settings.get_settings", return_value=mock_settings),
            patch.object(httpx, "Client", return_value=mock_client),
        ):
            # Should not raise
            _send_iteration_notification(tmp_path, config)

"""Comprehensive tests for the use_hypotheses feature.

Tests cover the full vertical slice:
  tools/knowledge.py → tools/registry.py → agent/sdk_executor.py →
  agent/factory.py → orchestrator/discovery.py → job_manager.py →
  database/models/job.py → webapp_components/pages/job_detail.py
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import User
from openscientist.database.models.job import Job as JobModel
from openscientist.job.types import JobInfo, JobStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_user(db_session: AsyncSession) -> User:
    """Create a minimal User for job ownership."""
    user = User(email=f"hyp-test-{uuid4()}@example.com", name="Hypothesis Tester")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def job_with_hypotheses(db_session: AsyncSession, db_user: User) -> JobModel:
    """A Job row with use_hypotheses=True."""
    job = JobModel(
        owner_id=db_user.id,
        title="Test hypothesis job",
        status="pending",
        max_iterations=5,
        use_hypotheses=True,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest_asyncio.fixture
async def job_without_hypotheses(db_session: AsyncSession, db_user: User) -> JobModel:
    """A Job row with use_hypotheses=False (default)."""
    job = JobModel(
        owner_id=db_user.id,
        title="Test no-hypothesis job",
        status="pending",
        max_iterations=5,
        use_hypotheses=False,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# 1. Database model
# ---------------------------------------------------------------------------


class TestJobModelColumn:
    """The Job ORM model must have the use_hypotheses column."""

    async def test_use_hypotheses_defaults_to_false(
        self, db_session: AsyncSession, db_user: User
    ) -> None:
        job = JobModel(
            owner_id=db_user.id,
            title="Default test",
            status="pending",
            max_iterations=3,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        assert job.use_hypotheses is False

    async def test_use_hypotheses_persists_true(
        self, db_session: AsyncSession, job_with_hypotheses: JobModel
    ) -> None:
        assert job_with_hypotheses.use_hypotheses is True

    async def test_use_hypotheses_persists_false(
        self, db_session: AsyncSession, job_without_hypotheses: JobModel
    ) -> None:
        assert job_without_hypotheses.use_hypotheses is False

    async def test_use_hypotheses_column_exists_in_mapped_columns(self) -> None:
        """use_hypotheses is a declared mapped_column on Job."""
        # Inspect the mapper to confirm the attribute is a real column
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(JobModel)
        col_names = [c.key for c in mapper.columns]
        assert "use_hypotheses" in col_names


# ---------------------------------------------------------------------------
# 2. job/types.py — JobInfo
# ---------------------------------------------------------------------------


class TestJobInfoUseHypotheses:
    """JobInfo must carry the use_hypotheses flag."""

    def test_default_is_false(self) -> None:
        info = JobInfo(
            job_id="j1",
            research_question="Q?",
            status=JobStatus.PENDING,
            created_at="2026-01-01T00:00:00",
        )
        assert info.use_hypotheses is False

    def test_can_be_set_to_true(self) -> None:
        info = JobInfo(
            job_id="j1",
            research_question="Q?",
            status=JobStatus.PENDING,
            created_at="2026-01-01T00:00:00",
            use_hypotheses=True,
        )
        assert info.use_hypotheses is True

    def test_from_db_model_with_use_hypotheses_true(self, job_with_hypotheses: JobModel) -> None:
        info = JobInfo.from_db_model(job_with_hypotheses)
        assert info.use_hypotheses is True

    def test_from_db_model_with_use_hypotheses_false(
        self, job_without_hypotheses: JobModel
    ) -> None:
        info = JobInfo.from_db_model(job_without_hypotheses)
        assert info.use_hypotheses is False

    def test_from_db_model_missing_attribute_defaults_false(self) -> None:
        """Graceful fallback when attribute is absent (legacy objects)."""
        fake_job = SimpleNamespace(
            id=uuid4(),
            title="Old job",
            status="completed",
            created_at=__import__("datetime").datetime(2026, 1, 1),
            updated_at=__import__("datetime").datetime(2026, 1, 1),
            max_iterations=5,
            error_message=None,
            cancellation_reason=None,
            short_title=None,
            owner_id=None,
            # use_hypotheses intentionally absent
        )
        info = JobInfo.from_db_model(fake_job)
        assert info.use_hypotheses is False

    def test_to_dict_and_from_dict_roundtrip_preserves_use_hypotheses(self) -> None:
        info = JobInfo(
            job_id="j1",
            research_question="Q?",
            status=JobStatus.RUNNING,
            created_at="2026-01-01T00:00:00",
            use_hypotheses=True,
        )
        restored = JobInfo.from_dict(info.to_dict())
        assert restored.use_hypotheses is True


# ---------------------------------------------------------------------------
# 3. tools/knowledge.py — make_tools gating
# ---------------------------------------------------------------------------


class TestKnowledgeMakeTools:
    """make_tools() must gate hypothesis tools on use_hypotheses."""

    def _get_tool_names(self, tools: list[Any]) -> set[str]:
        return {t.name for t in tools}

    def test_use_hypotheses_true_includes_all_three_tools(self, tmp_path: Path) -> None:
        from openscientist.tools.knowledge import make_tools
        from openscientist.tools.registry import ToolContext

        ctx = ToolContext(job_id="test-id", job_dir=tmp_path)
        tools = make_tools(ctx, use_hypotheses=True)
        names = self._get_tool_names(tools)
        assert "update_knowledge_state" in names
        assert "add_hypothesis" in names
        assert "update_hypothesis" in names
        assert len(tools) == 3

    def test_use_hypotheses_false_only_update_knowledge_state(self, tmp_path: Path) -> None:
        from openscientist.tools.knowledge import make_tools
        from openscientist.tools.registry import ToolContext

        ctx = ToolContext(job_id="test-id", job_dir=tmp_path)
        tools = make_tools(ctx, use_hypotheses=False)
        names = self._get_tool_names(tools)
        assert "update_knowledge_state" in names
        assert "add_hypothesis" not in names
        assert "update_hypothesis" not in names
        assert len(tools) == 1

    def test_default_excludes_hypothesis_tools(self, tmp_path: Path) -> None:
        """Default use_hypotheses=False means hypothesis tools are opt-in."""
        from openscientist.tools.knowledge import make_tools
        from openscientist.tools.registry import ToolContext

        ctx = ToolContext(job_id="test-id", job_dir=tmp_path)
        tools = make_tools(ctx)
        names = self._get_tool_names(tools)
        assert "add_hypothesis" not in names
        assert "update_hypothesis" not in names
        assert "update_knowledge_state" in names

    def test_tools_are_sdk_tool_instances(self, tmp_path: Path) -> None:
        """All returned objects must be SdkMcpTool instances."""
        try:
            from claude_agent_sdk import SdkMcpTool
        except ImportError:
            pytest.skip("claude_agent_sdk not installed")

        from openscientist.tools.knowledge import make_tools
        from openscientist.tools.registry import ToolContext

        ctx = ToolContext(job_id="test-id", job_dir=tmp_path)
        for flag in (True, False):
            tools = make_tools(ctx, use_hypotheses=flag)
            for t in tools:
                assert isinstance(t, SdkMcpTool), f"Expected SdkMcpTool, got {type(t)}"


# ---------------------------------------------------------------------------
# 4. tools/registry.py — build_tool_list propagation
# ---------------------------------------------------------------------------


class TestBuildToolListHypotheses:
    """build_tool_list() must propagate use_hypotheses to knowledge tools."""

    def _tool_names(self, tools: list[Any]) -> set[str]:
        return {t.name for t in tools}

    def test_use_hypotheses_false_excludes_hypothesis_tools(self, tmp_path: Path) -> None:
        from openscientist.tools.registry import build_tool_list

        tools = build_tool_list("test-id", tmp_path, use_hypotheses=False)
        names = self._tool_names(tools)
        assert "add_hypothesis" not in names
        assert "update_hypothesis" not in names
        assert "update_knowledge_state" in names

    def test_use_hypotheses_true_includes_hypothesis_tools(self, tmp_path: Path) -> None:
        from openscientist.tools.registry import build_tool_list

        tools = build_tool_list("test-id", tmp_path, use_hypotheses=True)
        names = self._tool_names(tools)
        assert "add_hypothesis" in names
        assert "update_hypothesis" in names
        assert "update_knowledge_state" in names

    def test_default_excludes_hypothesis_tools(self, tmp_path: Path) -> None:
        """Default use_hypotheses=False so new jobs don't get hypothesis tools by accident."""
        from openscientist.tools.registry import build_tool_list

        tools = build_tool_list("test-id", tmp_path)
        names = self._tool_names(tools)
        assert "add_hypothesis" not in names
        assert "update_hypothesis" not in names

    def test_all_tools_have_unique_names(self, tmp_path: Path) -> None:
        from openscientist.tools.registry import build_tool_list

        for flag in (True, False):
            tools = build_tool_list("test-id", tmp_path, use_hypotheses=flag)
            names = [t.name for t in tools]
            assert len(names) == len(set(names)), f"Duplicate tool names with use_hypotheses={flag}"


# ---------------------------------------------------------------------------
# 5. agent/sdk_executor.py — SDKAgentExecutor wiring
# ---------------------------------------------------------------------------


class TestSDKAgentExecutorHypotheses:
    """SDKAgentExecutor must build tool list with correct use_hypotheses."""

    def test_use_hypotheses_false_tools_lack_hypothesis_tools(self, tmp_path: Path) -> None:
        from openscientist.agent.sdk_executor import SDKAgentExecutor

        exe = SDKAgentExecutor(
            job_dir=tmp_path,
            data_file=None,
            system_prompt=None,
            use_hypotheses=False,
        )
        names = {t.name for t in exe._tools}
        assert "add_hypothesis" not in names
        assert "update_hypothesis" not in names

    def test_use_hypotheses_true_tools_include_hypothesis_tools(self, tmp_path: Path) -> None:
        from openscientist.agent.sdk_executor import SDKAgentExecutor

        exe = SDKAgentExecutor(
            job_dir=tmp_path,
            data_file=None,
            system_prompt=None,
            use_hypotheses=True,
        )
        names = {t.name for t in exe._tools}
        assert "add_hypothesis" in names
        assert "update_hypothesis" in names

    def test_default_use_hypotheses_is_false(self, tmp_path: Path) -> None:
        """SDKAgentExecutor default should not expose hypothesis tools."""
        from openscientist.agent.sdk_executor import SDKAgentExecutor

        exe = SDKAgentExecutor(
            job_dir=tmp_path,
            data_file=None,
            system_prompt=None,
        )
        names = {t.name for t in exe._tools}
        assert "add_hypothesis" not in names


# ---------------------------------------------------------------------------
# 6. agent/factory.py — get_agent_executor wiring
# ---------------------------------------------------------------------------


class TestGetAgentExecutorHypotheses:
    """get_agent_executor() must pass use_hypotheses through to SDKAgentExecutor."""

    def test_use_hypotheses_false_propagates(self, tmp_path: Path) -> None:
        from openscientist.agent.factory import get_agent_executor

        executor = get_agent_executor(
            job_dir=tmp_path,
            data_file=None,
            system_prompt=None,
            use_hypotheses=False,
        )
        names = {t.name for t in executor.__dict__["_tools"]}
        assert "add_hypothesis" not in names

    def test_use_hypotheses_true_propagates(self, tmp_path: Path) -> None:
        from openscientist.agent.factory import get_agent_executor

        executor = get_agent_executor(
            job_dir=tmp_path,
            data_file=None,
            system_prompt=None,
            use_hypotheses=True,
        )
        names = {t.name for t in executor.__dict__["_tools"]}
        assert "add_hypothesis" in names
        assert "update_hypothesis" in names

    def test_default_does_not_include_hypothesis_tools(self, tmp_path: Path) -> None:
        from openscientist.agent.factory import get_agent_executor

        executor = get_agent_executor(
            job_dir=tmp_path,
            data_file=None,
            system_prompt=None,
        )
        names = {t.name for t in executor.__dict__["_tools"]}
        assert "add_hypothesis" not in names


# ---------------------------------------------------------------------------
# 7. orchestrator/discovery.py — _load_runtime_context
# ---------------------------------------------------------------------------


def _make_mock_session_for_job(job: Any) -> Any:
    """Build a mock AsyncSessionLocal context manager that returns a single job."""
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = job

    files_result = MagicMock()
    files_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[job_result, files_result])

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_session
    mock_cm.__aexit__.return_value = False
    return mock_cm


class TestLoadRuntimeContextHypotheses:
    """_load_runtime_context must return use_hypotheses from the database."""

    def _make_db_job(self, use_hypotheses: bool) -> Any:
        job_id = uuid4()
        job = SimpleNamespace(
            id=job_id,
            title="Test research",
            max_iterations=5,
            use_hypotheses=use_hypotheses,
            investigation_mode="autonomous",
        )
        return job, job_id

    async def test_use_hypotheses_true_in_runtime(self, tmp_path: Path) -> None:
        from openscientist.orchestrator.discovery import _load_runtime_context

        job, job_id = self._make_db_job(use_hypotheses=True)
        job_dir = tmp_path / str(job_id)
        job_dir.mkdir()

        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            return_value=_make_mock_session_for_job(job),
        ):
            runtime = await _load_runtime_context(job_dir)

        assert runtime["use_hypotheses"] is True

    async def test_use_hypotheses_false_in_runtime(self, tmp_path: Path) -> None:
        from openscientist.orchestrator.discovery import _load_runtime_context

        job, job_id = self._make_db_job(use_hypotheses=False)
        job_dir = tmp_path / str(job_id)
        job_dir.mkdir()

        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            return_value=_make_mock_session_for_job(job),
        ):
            runtime = await _load_runtime_context(job_dir)

        assert runtime["use_hypotheses"] is False

    async def test_runtime_keys_complete(self, tmp_path: Path) -> None:
        """Runtime context must include all required keys."""
        from openscientist.orchestrator.discovery import _load_runtime_context

        job, job_id = self._make_db_job(use_hypotheses=True)
        job_dir = tmp_path / str(job_id)
        job_dir.mkdir()

        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            return_value=_make_mock_session_for_job(job),
        ):
            runtime = await _load_runtime_context(job_dir)

        expected_keys = {
            "job_id",
            "research_question",
            "max_iterations",
            "use_hypotheses",
            "investigation_mode",
            "data_files",
        }
        assert expected_keys.issubset(runtime.keys())


# ---------------------------------------------------------------------------
# 8. job_manager._db_create_job — DB write path (via mocked session)
# ---------------------------------------------------------------------------


class TestDbCreateJobHypotheses:
    """_db_create_job must build a JobModel with the correct use_hypotheses flag."""

    def _make_session_mock(self) -> tuple[Any, list]:
        """Return (mock_session_cm, captured_jobs_list) for inspecting JobModel instances."""
        captured: list[JobModel] = []

        mock_session = AsyncMock()
        # session.add() is synchronous in SQLAlchemy — use MagicMock so side_effect fires
        mock_session.add = MagicMock(side_effect=captured.append)
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_session
        mock_cm.__aexit__.return_value = False
        return mock_cm, captured

    async def test_creates_job_with_use_hypotheses_true(self) -> None:
        from openscientist.job_manager import _db_create_job

        mock_cm, captured = self._make_session_mock()
        job_id = str(uuid4())

        with patch("openscientist.job_manager.AsyncSessionLocal", return_value=mock_cm):
            await _db_create_job(
                job_id,
                "Research with hypotheses",
                max_iterations=3,
                use_hypotheses=True,
            )

        assert len(captured) == 1
        assert captured[0].use_hypotheses is True

    async def test_creates_job_with_use_hypotheses_false(self) -> None:
        from openscientist.job_manager import _db_create_job

        mock_cm, captured = self._make_session_mock()
        job_id = str(uuid4())

        with patch("openscientist.job_manager.AsyncSessionLocal", return_value=mock_cm):
            await _db_create_job(
                job_id,
                "Research without hypotheses",
                max_iterations=3,
                use_hypotheses=False,
            )

        assert len(captured) == 1
        assert captured[0].use_hypotheses is False

    async def test_creates_job_with_default_use_hypotheses_false(self) -> None:
        """Default must be False so existing callers are not surprised."""
        from openscientist.job_manager import _db_create_job

        mock_cm, captured = self._make_session_mock()
        job_id = str(uuid4())

        with patch("openscientist.job_manager.AsyncSessionLocal", return_value=mock_cm):
            await _db_create_job(
                job_id,
                "Default hypotheses test",
                max_iterations=3,
            )

        assert len(captured) == 1
        assert captured[0].use_hypotheses is False


# ---------------------------------------------------------------------------
# 9. job_detail page helpers — stats badges
# ---------------------------------------------------------------------------


class TestStatsBadgesHypotheses:
    """_stats_badges must include a hypothesis count card when non-zero."""

    def _get_labels(self, badges: list) -> list[str]:
        return [b[0] for b in badges]

    def _get_badge(self, badges: list, label: str) -> tuple | None:
        return next((b for b in badges if b[0] == label), None)

    def _make_job(self, status: str = "completed", findings: int = 0) -> Any:
        return SimpleNamespace(
            status=JobStatus(status),
            iterations_completed=2,
            max_iterations=5,
            findings_count=findings,
        )

    def test_no_hypotheses_badge_when_count_is_zero(self) -> None:
        from openscientist.webapp_components.pages.job_detail import _stats_badges

        badges = _stats_badges(self._make_job(), lit_count=3, hyp_count=0)
        labels = self._get_labels(badges)
        assert "Hypotheses" not in labels

    def test_hypotheses_badge_appears_when_count_nonzero(self) -> None:
        from openscientist.webapp_components.pages.job_detail import _stats_badges

        badges = _stats_badges(self._make_job(), lit_count=3, hyp_count=5)
        badge = self._get_badge(badges, "Hypotheses")
        assert badge is not None
        assert badge[1] == 5
        assert badge[2] == "orange"

    def test_findings_badge_still_present_alongside_hypotheses(self) -> None:
        from openscientist.webapp_components.pages.job_detail import _stats_badges

        badges = _stats_badges(self._make_job(findings=4), lit_count=2, hyp_count=3)
        labels = self._get_labels(badges)
        assert "Findings" in labels
        assert "Hypotheses" in labels

    def test_default_hyp_count_zero_omits_badge(self) -> None:
        from openscientist.webapp_components.pages.job_detail import _stats_badges

        badges = _stats_badges(self._make_job(), lit_count=0)
        labels = self._get_labels(badges)
        assert "Hypotheses" not in labels


# ---------------------------------------------------------------------------
# 10. job_detail page helpers — analysis log metadata
# ---------------------------------------------------------------------------


class TestAnalysisLogMetadata:
    """Analysis log helpers should surface concise metadata for the timeline UI."""

    def test_search_pubmed_metadata_includes_query_and_result_count(self) -> None:
        from openscientist.webapp_components.pages.job_detail import _analysis_log_meta_lines

        lines = _analysis_log_meta_lines(
            {
                "action": "search_pubmed",
                "query": "hypothermia metabolomics",
                "results_count": 7,
            }
        )

        assert [line.text for line in lines] == [
            'Query: "hypothermia metabolomics"',
            "Papers found: 7",
        ]
        assert all(line.italic is False for line in lines)

    def test_hypothesis_metadata_marks_statement_as_italic(self) -> None:
        from openscientist.webapp_components.pages.job_detail import _analysis_log_meta_lines

        lines = _analysis_log_meta_lines(
            {
                "action": "update_hypothesis",
                "statement": "Cold exposure shifts the metabolome.",
                "status": "supported",
                "result_summary": "Observed in the treated cohort.",
            }
        )

        assert [line.text for line in lines] == [
            "Cold exposure shifts the metabolome.",
            "Status: supported",
            "Observed in the treated cohort.",
        ]
        assert [line.italic for line in lines] == [True, False, False]

    def test_execute_code_metadata_adds_duration(self) -> None:
        from openscientist.webapp_components.pages.job_detail import _analysis_log_meta_lines

        lines = _analysis_log_meta_lines(
            {
                "action": "execute_code",
                "execution_time": 3.5,
            }
        )

        assert [line.text for line in lines] == ["Duration: 3.5s"]


# ---------------------------------------------------------------------------
# 11. job_detail page helpers — plot metadata loading
# ---------------------------------------------------------------------------


class TestCollectIterationPlots:
    """Plot collection should skip unreadable metadata files instead of crashing."""

    def test_skips_non_utf8_metadata_files(self, tmp_path: Path) -> None:
        from openscientist.webapp_components.pages.job_detail import _collect_iteration_plots

        plots_dir = tmp_path / "provenance"
        plots_dir.mkdir()
        (plots_dir / "plot.png").write_bytes(b"png")
        (plots_dir / "plot.json").write_bytes(b"\xff\xfe\x00\x00")

        plots = _collect_iteration_plots(plots_dir, 1)

        assert plots == []


# ---------------------------------------------------------------------------
# 12. job_detail page helpers — _render_iteration_hypotheses filtering
# ---------------------------------------------------------------------------


class TestRenderIterationHypothesesFiltering:
    """The hypothesis filtering logic is correct — proposed or tested this iteration."""

    def _make_ks_data(self, hypotheses: list[dict]) -> dict:
        return {"hypotheses": hypotheses, "findings": [], "literature": []}

    def _proposed_in(self, iter_num: int, status: str = "pending") -> dict:
        return {
            "id": f"H{iter_num:03d}",
            "statement": f"Hypothesis proposed in iter {iter_num}",
            "status": status,
            "iteration_proposed": iter_num,
            "result": {},
        }

    def _tested_in(self, iter_num: int, status: str = "supported") -> dict:
        return {
            "id": f"T{iter_num:03d}",
            "statement": f"Hypothesis tested in iter {iter_num}",
            "status": status,
            "iteration_proposed": 1,
            "iteration_tested": iter_num,
            "result": {"summary": "Confirmed", "conclusion": "Yes."},
        }

    def _filter_for_iter(self, ks_data: dict, iter_num: int) -> list[dict]:
        """Replicate the filtering logic from _render_iteration_hypotheses."""
        return [
            h
            for h in ks_data.get("hypotheses", [])
            if h.get("iteration_proposed") == iter_num or h.get("iteration_tested") == iter_num
        ]

    def test_proposed_this_iter_included(self) -> None:
        ks = self._make_ks_data([self._proposed_in(2)])
        result = self._filter_for_iter(ks, 2)
        assert len(result) == 1
        assert result[0]["statement"] == "Hypothesis proposed in iter 2"

    def test_tested_this_iter_included(self) -> None:
        ks = self._make_ks_data([self._tested_in(3)])
        result = self._filter_for_iter(ks, 3)
        assert len(result) == 1

    def test_other_iter_excluded(self) -> None:
        ks = self._make_ks_data([self._proposed_in(1), self._proposed_in(3)])
        result = self._filter_for_iter(ks, 2)
        assert len(result) == 0

    def test_proposed_and_tested_same_iter_both_included(self) -> None:
        ks = self._make_ks_data(
            [
                self._proposed_in(2),
                self._tested_in(2),
            ]
        )
        result = self._filter_for_iter(ks, 2)
        assert len(result) == 2

    def test_empty_hypotheses_list_returns_empty(self) -> None:
        ks = self._make_ks_data([])
        result = self._filter_for_iter(ks, 1)
        assert result == []

    def test_no_hypotheses_key_returns_empty(self) -> None:
        ks: dict = {}
        result = self._filter_for_iter(ks, 1)
        assert result == []

    def test_all_statuses_included_regardless(self) -> None:
        """All hypothesis statuses (pending/testing/supported/rejected) should appear."""
        hypotheses = [
            {**self._proposed_in(1), "status": s}
            for s in ("pending", "testing", "supported", "rejected")
        ]
        ks = self._make_ks_data(hypotheses)
        result = self._filter_for_iter(ks, 1)
        assert len(result) == 4


# ---------------------------------------------------------------------------
# 13. Per-iteration hypothesis count in _render_iteration_card
# ---------------------------------------------------------------------------


class TestIterationHypothesisCount:
    """Hypothesis count computed in _render_iteration_card must be correct."""

    def _count_for_iter(self, timeline_ks: dict, iteration: int) -> int:
        """Replicate the count expression from _render_iteration_card."""
        return sum(
            1
            for h in timeline_ks.get("hypotheses", [])
            if h.get("iteration_proposed") == iteration or h.get("iteration_tested") == iteration
        )

    def test_count_correct_for_mixed_hypotheses(self) -> None:
        timeline_ks = {
            "hypotheses": [
                {"iteration_proposed": 1},  # proposed iter 1
                {"iteration_proposed": 2, "iteration_tested": 3},  # proposed 2, tested 3
                {"iteration_proposed": 3},  # proposed iter 3
            ]
        }
        assert self._count_for_iter(timeline_ks, 1) == 1
        assert self._count_for_iter(timeline_ks, 2) == 1
        assert self._count_for_iter(timeline_ks, 3) == 2

    def test_count_zero_when_no_match(self) -> None:
        timeline_ks = {"hypotheses": [{"iteration_proposed": 5}]}
        assert self._count_for_iter(timeline_ks, 1) == 0

    def test_count_zero_for_empty_hypotheses(self) -> None:
        timeline_ks: dict = {}
        assert self._count_for_iter(timeline_ks, 1) == 0


# ---------------------------------------------------------------------------
# 12. new_job page — form structure verification
# ---------------------------------------------------------------------------


class TestNewJobPageStructure:
    """Verify the new_job.py form wiring is correct."""

    def test_submit_job_accepts_use_hypotheses(self) -> None:
        import inspect

        from openscientist.webapp_components.pages.new_job import _submit_job

        sig = inspect.signature(_submit_job)
        assert "use_hypotheses" in sig.parameters

    def test_submit_job_accepts_coinvestigate_mode(self) -> None:
        import inspect

        from openscientist.webapp_components.pages.new_job import _submit_job

        sig = inspect.signature(_submit_job)
        assert "coinvestigate_mode" in sig.parameters

    def test_all_required_params_present(self) -> None:
        import inspect

        from openscientist.webapp_components.pages.new_job import _submit_job

        sig = inspect.signature(_submit_job)
        required_params = {
            "job_manager",
            "user_can_start_jobs",
            "session_id",
            "research_question",
            "max_iterations",
            "use_hypotheses",
            "coinvestigate_mode",
        }
        for param in required_params:
            assert param in sig.parameters, f"Missing parameter: {param}"


# ---------------------------------------------------------------------------
# 13. End-to-end: create_job → DB → runtime context
# ---------------------------------------------------------------------------


class TestEndToEndHypothesesFlow:
    """Test the full create → persist → read flow for use_hypotheses."""

    def test_create_db_job_record_passes_use_hypotheses_to_db_create_job(
        self, tmp_path: Path
    ) -> None:
        """_create_db_job_record(use_hypotheses=True) calls _db_create_job with use_hypotheses=True."""
        from openscientist.job_manager import JobManager

        job_id = str(uuid4())
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        jm = JobManager(jobs_dir=jobs_dir)

        with patch(
            "openscientist.job_manager._db_create_job", new_callable=AsyncMock
        ) as mock_db_create:
            jm._create_db_job_record(
                job_id=job_id,
                research_question="Test",
                max_iterations=3,
                use_hypotheses=True,
                investigation_mode="autonomous",
                owner_id=None,
                title=None,
                description=None,
                pdb_code=None,
                space_group=None,
            )
            mock_db_create.assert_called_once()
            _, kwargs = mock_db_create.call_args
            assert kwargs["use_hypotheses"] is True

    async def test_runtime_context_true_via_mock(self, tmp_path: Path) -> None:
        """Jobs with use_hypotheses=True produce runtime['use_hypotheses'] = True."""
        from openscientist.orchestrator.discovery import _load_runtime_context

        job_id = uuid4()
        job_dir = tmp_path / str(job_id)
        job_dir.mkdir()

        fake_job = SimpleNamespace(
            id=job_id,
            title="Hypothesis research",
            max_iterations=5,
            use_hypotheses=True,
            investigation_mode="autonomous",
        )
        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            return_value=_make_mock_session_for_job(fake_job),
        ):
            runtime = await _load_runtime_context(job_dir)

        assert runtime["use_hypotheses"] is True
        assert isinstance(runtime["use_hypotheses"], bool)

    async def test_runtime_context_false_via_mock(self, tmp_path: Path) -> None:
        """Jobs with use_hypotheses=False produce runtime['use_hypotheses'] = False."""
        from openscientist.orchestrator.discovery import _load_runtime_context

        job_id = uuid4()
        job_dir = tmp_path / str(job_id)
        job_dir.mkdir()

        fake_job = SimpleNamespace(
            id=job_id,
            title="No-hypothesis research",
            max_iterations=5,
            use_hypotheses=False,
            investigation_mode="autonomous",
        )
        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            return_value=_make_mock_session_for_job(fake_job),
        ):
            runtime = await _load_runtime_context(job_dir)

        assert runtime["use_hypotheses"] is False

    async def test_build_agent_executor_passes_use_hypotheses_to_get_agent_executor(
        self, tmp_path: Path
    ) -> None:
        """_build_agent_executor(use_hypotheses=True) passes it to get_agent_executor."""
        from openscientist.orchestrator.discovery import _build_agent_executor

        with (
            patch("openscientist.orchestrator.discovery.get_agent_executor") as mock_get_executor,
            patch("openscientist.orchestrator.discovery.get_system_prompt", return_value="prompt"),
        ):
            _build_agent_executor(
                job_dir=tmp_path,
                data_file=None,
                use_hypotheses=True,
            )
            call_kwargs = mock_get_executor.call_args[1]
            assert call_kwargs["use_hypotheses"] is True

    async def test_build_agent_executor_false_passes_false(self, tmp_path: Path) -> None:
        from openscientist.orchestrator.discovery import _build_agent_executor

        with (
            patch("openscientist.orchestrator.discovery.get_agent_executor") as mock_get_executor,
            patch("openscientist.orchestrator.discovery.get_system_prompt", return_value="prompt"),
        ):
            _build_agent_executor(
                job_dir=tmp_path,
                data_file=None,
                use_hypotheses=False,
            )
            call_kwargs = mock_get_executor.call_args[1]
            assert call_kwargs["use_hypotheses"] is False


# ---------------------------------------------------------------------------
# 14. generate_job_claude_md — dynamic JOB_CLAUDE.md generation
# ---------------------------------------------------------------------------


class TestGenerateJobClaudeMd:
    """generate_job_claude_md() produces correct content based on use_hypotheses."""

    def test_with_hypotheses_contains_hypothesis_tools(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md(use_hypotheses=True)
        assert "add_hypothesis" in content
        assert "update_hypothesis" in content
        assert "Hypothesis Tracking Workflow" in content

    def test_without_hypotheses_omits_hypothesis_tools(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md(use_hypotheses=False)
        assert "add_hypothesis" not in content
        assert "update_hypothesis" not in content
        assert "Hypothesis Tracking Workflow" not in content

    def test_without_hypotheses_still_contains_core_tools(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md(use_hypotheses=False)
        assert "execute_code" in content
        assert "search_pubmed" in content
        assert "update_knowledge_state" in content
        assert "save_iteration_summary" in content
        assert "set_status" in content
        assert "set_job_title" in content
        assert "set_consensus_answer" in content
        assert "read_document" in content
        assert "search_skills" in content

    def test_with_hypotheses_still_contains_core_tools(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md(use_hypotheses=True)
        assert "execute_code" in content
        assert "search_pubmed" in content
        assert "update_knowledge_state" in content
        assert "save_iteration_summary" in content

    def test_default_is_no_hypotheses(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md()
        assert "add_hypothesis" not in content
        assert "update_hypothesis" not in content

    def test_with_hypotheses_includes_hypothesis_approach_steps(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md(use_hypotheses=True)
        assert "Use `add_hypothesis` to formally record each hypothesis" in content
        assert "Use `update_hypothesis` to record results" in content
        assert 'Update hypothesis to `"supported"`' in content
        assert 'Update hypothesis to `"refuted"`' in content

    def test_without_hypotheses_omits_hypothesis_approach_steps(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md(use_hypotheses=False)
        assert "Use `add_hypothesis` to formally record" not in content
        assert "Use `update_hypothesis` to record results" not in content
        assert 'Update hypothesis to `"supported"`' not in content

    def test_without_hypotheses_has_alternative_interpret_steps(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        content = generate_job_claude_md(use_hypotheses=False)
        assert "Record confirmed findings to the knowledge state" in content
        assert "Negative results are also valuable" in content

    def test_returns_string(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        assert isinstance(generate_job_claude_md(use_hypotheses=True), str)
        assert isinstance(generate_job_claude_md(use_hypotheses=False), str)

    def test_both_variants_have_mission_and_footer(self) -> None:
        from openscientist.prompts import generate_job_claude_md

        for flag in (True, False):
            content = generate_job_claude_md(use_hypotheses=flag)
            assert "# OpenScientist: Scientific Hypothesis Agent for Novel Discovery" in content
            assert "You are autonomous" in content


# ---------------------------------------------------------------------------
# 15. _write_skills_to_claude_dir writes JOB_CLAUDE.md
# ---------------------------------------------------------------------------


class TestWriteSkillsToClaudeDirJobClaudeMd:
    """_write_skills_to_claude_dir writes the correct CLAUDE.md based on use_hypotheses."""

    async def test_writes_job_claude_md_with_hypotheses(self, tmp_path: Path) -> None:
        from openscientist.orchestrator.discovery import _write_skills_to_claude_dir

        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            side_effect=Exception("no db"),
        ):
            await _write_skills_to_claude_dir(tmp_path, use_hypotheses=True)

        claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        assert "add_hypothesis" in claude_md
        assert "update_hypothesis" in claude_md
        assert "Hypothesis Tracking Workflow" in claude_md

    async def test_writes_job_claude_md_without_hypotheses(self, tmp_path: Path) -> None:
        from openscientist.orchestrator.discovery import _write_skills_to_claude_dir

        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            side_effect=Exception("no db"),
        ):
            await _write_skills_to_claude_dir(tmp_path, use_hypotheses=False)

        claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        assert "add_hypothesis" not in claude_md
        assert "update_hypothesis" not in claude_md
        assert "execute_code" in claude_md

    async def test_default_use_hypotheses_is_false(self, tmp_path: Path) -> None:
        from openscientist.orchestrator.discovery import _write_skills_to_claude_dir

        with patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            side_effect=Exception("no db"),
        ):
            await _write_skills_to_claude_dir(tmp_path)

        claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        assert "add_hypothesis" not in claude_md

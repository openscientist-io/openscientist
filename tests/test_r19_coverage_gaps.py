"""Focused R19 coverage tests for priority risk / error-handling paths."""

from __future__ import annotations

import asyncio
import builtins
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from openscientist.code_executor import (
    CodeExecutionTimeoutError,
    ForbiddenImportError,
    _safe_import,
    execute_rust_code,
    load_data,
    timeout_handler,
)
from openscientist.database.models import SkillSource
from openscientist.exceptions import ProviderError
from openscientist.job.types import JobInfo, JobStatus
from openscientist.pdf_generator import ReportPDF
from openscientist.skill_scheduler import (
    SkillSyncScheduler,
    SyncResult,
    get_scheduler,
    start_skill_scheduler,
    stop_skill_scheduler,
)


class TestCodeExecutorSecurityAndErrors:
    def test_relative_import_forbidden(self) -> None:
        with pytest.raises(ForbiddenImportError, match="Relative imports"):
            _safe_import("os", level=1)

    def test_timeout_handler_raises(self) -> None:
        with pytest.raises(CodeExecutionTimeoutError, match="timed out"):
            timeout_handler(0, None)

    def test_load_data_parquet_excel_json(self, tmp_path: Path) -> None:
        parquet = tmp_path / "a.parquet"
        xlsx = tmp_path / "a.xlsx"
        json_path = tmp_path / "a.json"
        parquet.write_bytes(b"x")
        xlsx.write_bytes(b"x")
        json_path.write_text("[]", encoding="utf-8")
        fake_df = MagicMock()
        with patch("openscientist.code_executor.pd.read_parquet", return_value=fake_df):
            assert load_data(str(parquet)) is fake_df
        with patch("openscientist.code_executor.pd.read_excel", return_value=fake_df):
            assert load_data(str(xlsx)) is fake_df
        with patch("openscientist.code_executor.pd.read_json", return_value=fake_df):
            assert load_data(str(json_path)) is fake_df

    def test_rust_cargo_missing_timeout_and_stderr(self, tmp_path: Path) -> None:
        plots = tmp_path / "plots"
        plots.mkdir()
        # execute_rust_code imports subprocess locally
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = execute_rust_code("fn main() {}", plots_dir=plots, timeout=1)
        assert result["success"] is False
        assert "cargo not found" in result["error"]

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="cargo", timeout=1),
        ):
            result = execute_rust_code("fn main() {}", plots_dir=plots, timeout=1)
        assert result["success"] is False
        assert "timed out" in result["error"]

        completed = MagicMock(returncode=1, stdout="out", stderr="compile error")
        with patch("subprocess.run", return_value=completed):
            result = execute_rust_code("fn main() {}", plots_dir=plots, timeout=5)
        assert result["success"] is False
        assert "compile error" in result["output"]


class TestReportPDFMethods:
    def test_report_pdf_formatting_methods(self) -> None:
        from fpdf import FPDF

        pdf = object.__new__(ReportPDF)
        FPDF.__init__(pdf)
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)

        with (
            patch.object(pdf, "set_font"),
            patch.object(pdf, "set_text_color"),
            patch.object(pdf, "multi_cell"),
            patch.object(pdf, "cell"),
            patch.object(pdf, "ln"),
            patch.object(pdf, "set_y"),
            patch.object(pdf, "page_no", return_value=2),
        ):
            pdf.header()
            pdf.footer()
            pdf.add_title("Title")
            pdf.add_heading_1("H1")
            pdf.add_heading_2("H2")
            pdf.add_heading_3("H3")
            pdf.add_paragraph("Paragraph text")
            pdf.add_code_block("print(1)")
            pdf.add_list_item("item", ordered=False)
            pdf.add_list_item("item", ordered=True)
            pdf.add_footer()
            with patch.object(pdf, "table") as table_cm:
                table_cm.return_value.__enter__ = MagicMock(return_value=MagicMock())
                table_cm.return_value.__exit__ = MagicMock(return_value=False)
                pdf.add_table([["a", "b"], ["1", "2"]])
                table_cm.assert_called()


class TestReportWeasyPdf:
    @pytest.mark.asyncio
    async def test_render_report_pdf_uses_weasyprint(self, tmp_path: Path) -> None:
        from openscientist.report.pdf import render_report_pdf

        html = tmp_path / "r.html"
        pdf_path = tmp_path / "r.pdf"
        html.write_text("<html></html>", encoding="utf-8")
        pdf_path.write_bytes(b"%PDF")

        mock_html = MagicMock()
        with patch.dict(
            sys.modules, {"weasyprint": MagicMock(HTML=MagicMock(return_value=mock_html))}
        ):
            # Import after patch so local import inside _render_pdf_sync sees mock
            with patch("weasyprint.HTML", return_value=mock_html):
                result = await render_report_pdf(html, pdf_path, tmp_path)

        mock_html.write_pdf.assert_called_once_with(str(pdf_path))
        assert result == pdf_path


class TestSkillSchedulerLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop_and_duplicate_start(self) -> None:
        scheduler = SkillSyncScheduler(sync_interval=3600, github_token="t")
        with patch.object(scheduler, "sync_all_sources", new_callable=AsyncMock):
            await scheduler.start()
            assert scheduler._running is True
            await scheduler.start()  # duplicate should no-op
            await scheduler.stop()
            assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_sync_all_sources_success_and_failure(self) -> None:
        scheduler = SkillSyncScheduler(sync_interval=3600, github_token="t")
        source_ok = cast(
            SkillSource,
            SimpleNamespace(id=uuid4(), name="ok", is_enabled=True),
        )
        source_bad = cast(
            SkillSource,
            SimpleNamespace(id=uuid4(), name="bad", is_enabled=True),
        )

        session = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__.return_value = session
        ctx.__aexit__.return_value = None

        ok = SyncResult(source_id=str(source_ok.id), source_name="ok", success=True, created=1)
        bad = SyncResult(
            source_id=str(source_bad.id),
            source_name="bad",
            success=False,
            error_message="boom",
        )

        with (
            patch("openscientist.skill_scheduler.get_admin_session", return_value=ctx),
            patch.object(
                scheduler,
                "_get_enabled_sources",
                new_callable=AsyncMock,
                return_value=[source_ok, source_bad],
            ),
            patch.object(
                scheduler,
                "_sync_source_if_needed",
                new_callable=AsyncMock,
                side_effect=[ok, bad],
            ),
        ):
            results = await scheduler.sync_all_sources()

        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is False
        assert results[1].error_message == "boom"

    @pytest.mark.asyncio
    async def test_global_scheduler_helpers(self) -> None:
        import openscientist.skill_scheduler as sched_mod

        sched_mod._scheduler = None
        with patch.object(SkillSyncScheduler, "start", new_callable=AsyncMock) as start:
            await start_skill_scheduler()
            start.assert_called_once()
        scheduler = get_scheduler()
        assert scheduler is get_scheduler()
        with patch.object(scheduler, "stop", new_callable=AsyncMock) as stop:
            await stop_skill_scheduler()
            stop.assert_called_once()
        assert sched_mod._scheduler is None


class TestHttpClientAuth:
    @pytest.mark.asyncio
    async def test_api_helpers_use_authenticated_client(self) -> None:
        from openscientist.webapp_components.utils import http_client

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=201))
        mock_client.delete = AsyncMock(return_value=MagicMock(status_code=204))

        class _CM:
            async def __aenter__(self) -> AsyncMock:
                return mock_client

            async def __aexit__(self, *args: object) -> None:
                return None

        with patch.object(http_client, "authenticated_client", return_value=_CM()):
            assert (await http_client.api_get("/x")).status_code == 200
            assert (await http_client.api_post("/x", json={"a": 1})).status_code == 201
            assert (await http_client.api_delete("/x")).status_code == 204


class TestOrchestratorPersistAndFeedback:
    def test_persist_data_files_sync_path(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import setup as setup_mod

        data = tmp_path / "data.csv"
        data.write_text("a,b\n1,2\n", encoding="utf-8")
        job_id = str(uuid4())

        def _fake_run(coro: object) -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                close()

        with (
            patch(
                "openscientist.orchestrator.setup.asyncio.get_running_loop",
                side_effect=RuntimeError,
            ),
            patch(
                "openscientist.orchestrator.setup.asyncio.run", side_effect=_fake_run
            ) as run_mock,
        ):
            setup_mod._persist_data_files_to_db(job_id, [data])
            run_mock.assert_called_once()

    def test_persist_data_files_background_schedule_error(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import setup as setup_mod

        data = tmp_path / "data.csv"
        data.write_text("a,b\n1,2\n", encoding="utf-8")

        def _fail_bg(coro: object, **_kwargs: object) -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError("bg fail")

        with (
            patch(
                "openscientist.orchestrator.setup.asyncio.get_running_loop",
                return_value=MagicMock(),
            ),
            patch(
                "openscientist.orchestrator.setup.create_background_task",
                side_effect=_fail_bg,
            ),
        ):
            setup_mod._persist_data_files_to_db(str(uuid4()), [data])

    @pytest.mark.asyncio
    async def test_wait_for_feedback_cancelled_continued_timeout(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import iteration as iteration_mod

        job_dir = tmp_path / str(uuid4())
        job_dir.mkdir()
        ks = MagicMock()
        ks.data = {"iteration": 1, "feedback_history": []}

        with (
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.load_from_database",
                new_callable=AsyncMock,
                return_value=ks,
            ),
            patch(
                "openscientist.orchestrator.iteration.get_job_status",
                new_callable=AsyncMock,
                return_value="cancelled",
            ),
            patch("openscientist.orchestrator.iteration.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await iteration_mod.wait_for_feedback_or_timeout(job_dir, timeout_seconds=60)
        assert result["outcome"] == "cancelled"

        with (
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.load_from_database",
                new_callable=AsyncMock,
                return_value=ks,
            ),
            patch(
                "openscientist.orchestrator.iteration.get_job_status",
                new_callable=AsyncMock,
                return_value="running",
            ),
            patch("openscientist.orchestrator.iteration.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await iteration_mod.wait_for_feedback_or_timeout(job_dir, timeout_seconds=60)
        assert result["outcome"] == "continued"

        with (
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.load_from_database",
                new_callable=AsyncMock,
                return_value=ks,
            ),
            patch(
                "openscientist.orchestrator.iteration.get_job_status",
                new_callable=AsyncMock,
                return_value="awaiting_feedback",
            ),
            patch("openscientist.orchestrator.iteration.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "openscientist.orchestrator.iteration.time.monotonic",
                side_effect=[0.0, 100.0],
            ),
        ):
            result = await iteration_mod.wait_for_feedback_or_timeout(job_dir, timeout_seconds=10)
        assert result["outcome"] == "timeout"


class TestBootstrapHelpers:
    def test_parse_datetime_and_load_json_errors(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from openscientist import bootstrap

        assert bootstrap._parse_datetime(datetime(2026, 1, 1, tzinfo=UTC)) is not None
        assert bootstrap._parse_datetime("2026-01-01T00:00:00Z") is not None
        assert bootstrap._parse_datetime("") is None
        assert bootstrap._parse_datetime(123) is None

        result = bootstrap.BootstrapResult()
        bad = tmp_path / "x.json"
        bad.write_text("[1,2,3]", encoding="utf-8")
        assert bootstrap._load_json(bad, result, "test") is None
        assert result.errors

    def test_write_json_oserror(self, tmp_path: Path) -> None:
        from openscientist import bootstrap

        result = bootstrap.BootstrapResult()
        target = tmp_path / "out.json"
        with patch("builtins.open", side_effect=OSError("disk full")):
            assert bootstrap._write_json(target, {"a": 1}, result, "test") is False
        assert result.errors


class TestVertexCostFailures:
    def test_get_cost_info_missing_credentials(self) -> None:
        from openscientist.providers.vertex import VertexProvider

        provider = object.__new__(VertexProvider)
        settings = MagicMock()
        settings.provider.google_application_credentials = None
        fake_cloud = MagicMock()
        fake_oauth2 = MagicMock()
        with (
            patch.dict(
                sys.modules,
                {
                    "google.cloud": fake_cloud,
                    "google.cloud.bigquery": MagicMock(),
                    "google.oauth2": fake_oauth2,
                    "google.oauth2.service_account": MagicMock(),
                },
            ),
            patch("openscientist.providers.vertex.get_settings", return_value=settings),
            pytest.raises(ProviderError, match="GOOGLE_APPLICATION_CREDENTIALS"),
        ):
            provider.get_cost_info()


class TestNtfyHelpers:
    def test_generate_topic_and_subscription_url(self) -> None:
        from uuid import UUID

        from openscientist import ntfy

        user_id = UUID("12345678-1234-5678-1234-567812345678")
        topic = ntfy.generate_topic_for_user(user_id)
        assert topic.startswith("openscientist-")
        assert topic in ntfy.get_subscription_url(topic)

    @pytest.mark.asyncio
    async def test_send_notification_empty_topic_and_post(self) -> None:
        from openscientist import ntfy

        assert await ntfy.send_notification("", "title", "body") is False

        with patch("openscientist.ntfy.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            response = MagicMock(status_code=200)
            response.raise_for_status = MagicMock()
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            client.post = AsyncMock(return_value=response)
            client_cls.return_value = client
            ok = await ntfy.send_notification("topic", "title", "body", tags=["rocket"])
        assert ok is True


class TestFoundryCostQuery:
    def test_query_azure_cost_usd_empty_and_value(self) -> None:
        from datetime import UTC, datetime

        from openscientist.providers import foundry as foundry_mod

        client = MagicMock()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)

        with patch.dict(
            sys.modules,
            {
                "azure.mgmt.costmanagement.models": MagicMock(
                    QueryAggregation=MagicMock(),
                    QueryDataset=MagicMock(),
                    QueryDefinition=MagicMock(),
                    QueryTimePeriod=MagicMock(),
                )
            },
        ):
            client.query.usage.return_value = MagicMock(rows=None)
            assert foundry_mod._query_azure_cost_usd(client, "subscriptions/x", start, end) == 0.0

            client.query.usage.return_value = MagicMock(rows=[[12.5]])
            assert foundry_mod._query_azure_cost_usd(client, "subscriptions/x", start, end) == 12.5


class TestDiscoveryPdfFallback:
    @pytest.mark.asyncio
    async def test_try_generate_report_pdf_weasy_then_fpdf_fallback(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import discovery

        report_path = tmp_path / "final_report.md"
        report_path.write_text("# Report\n", encoding="utf-8")

        with (
            patch(
                "openscientist.report.renderer.render_report_html",
                side_effect=RuntimeError("weasy html fail"),
            ),
            patch(
                "openscientist.pdf_generator.markdown_to_pdf",
                return_value=tmp_path / "final_report.pdf",
            ) as fpdf,
            patch(
                "openscientist.report.processor.strip_figure_tags",
                side_effect=lambda text: text,
            ),
        ):
            await discovery._try_generate_report_pdf(report_path)
            fpdf.assert_called_once()


class TestAnthropicSendMessageGuard:
    @pytest.mark.asyncio
    async def test_send_message_requires_api_key(self) -> None:
        from openscientist.providers.anthropic import AnthropicProvider

        provider = object.__new__(AnthropicProvider)
        settings = MagicMock()
        settings.provider.anthropic_api_key = None
        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=settings),
            pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"),
        ):
            await provider.send_message([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_send_message_success_path(self) -> None:
        from openscientist.providers.anthropic import AnthropicProvider

        provider = object.__new__(AnthropicProvider)
        settings = MagicMock()
        settings.provider.anthropic_api_key = "sk-test"
        settings.provider.model = "claude-test"
        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=settings),
            patch("anthropic.Anthropic") as client_cls,
            patch(
                "openscientist.providers.anthropic.send_anthropic_message",
                return_value="ok",
            ) as send,
        ):
            result = await provider.send_message([{"role": "user", "content": "hi"}])
        assert result == "ok"
        client_cls.assert_called_once()
        send.assert_called_once()


class TestBootstrapNormalizeHelpers:
    def test_status_paths_and_normalize_collections(self, tmp_path: Path) -> None:
        from openscientist import bootstrap

        assert bootstrap._normalize_status("") == "pending"
        assert bootstrap._normalize_status("COMPLETED") == "completed"
        assert bootstrap._normalize_status("weird") == "pending"

        job_id = uuid4()
        assert bootstrap._normalize_relative_path(job_id, "") == ""
        assert (
            bootstrap._normalize_relative_path(job_id, f"jobs/{job_id}/plots/a.png")
            == "plots/a.png"
        )
        assert (
            bootstrap._normalize_relative_path(job_id, "/jobs/legacy/plots/a.png") == "plots/a.png"
        )
        assert bootstrap._normalize_relative_path(job_id, "jobs/legacy") == "legacy"

        assert bootstrap._resolve_plot_path(tmp_path, None, "plots/x.png") == "plots/x.png"
        assert bootstrap._resolve_plot_path(tmp_path, None, None) is None
        (tmp_path / "plots").mkdir()
        (tmp_path / "plots" / "p.png").write_bytes(b"x")
        assert bootstrap._resolve_plot_path(tmp_path, "p.png", None) == "plots/p.png"
        (tmp_path / "provenance").mkdir()
        (tmp_path / "provenance" / "q.png").write_bytes(b"x")
        assert bootstrap._resolve_plot_path(tmp_path, "q.png", None) == "provenance/q.png"
        assert (
            bootstrap._resolve_plot_path(tmp_path, "missing.png", None) == "provenance/missing.png"
        )

        raw = {
            "hypotheses": [
                "skip",
                {"statement": ""},
                {"text": "H works", "iteration": 2, "status": "active"},
            ],
            "findings": [
                1,
                {"title": ""},
                {"content": "Finding", "iteration": 3, "plots": ["a.png"]},
            ],
            "literature": [
                None,
                {"title": ""},
                {"title": "Paper", "pmid": "1", "iteration": 1},
            ],
            "analysis_log": [
                "x",
                {"action": "run", "status": "failed", "custom": 1},
            ],
            "iteration_summaries": [
                "x",
                {"summary": ""},
                {"iteration": 1, "summary": "done", "strapline": "s"},
            ],
            "feedback_history": [
                "x",
                {"text": ""},
                {"feedback": "please continue", "after_iteration": 1},
            ],
        }
        hyps = bootstrap._normalize_hypotheses(raw)
        assert len(hyps) == 1 and hyps[0]["statement"] == "H works"
        findings = bootstrap._normalize_findings(raw)
        assert len(findings) == 1 and findings[0]["title"] == "Finding"
        lit = bootstrap._normalize_literature(raw)
        assert len(lit) == 1 and lit[0]["title"] == "Paper"
        logs = bootstrap._normalize_analysis_log(raw)
        assert len(logs) == 1 and logs[0]["success"] is False and logs[0]["custom"] == 1
        summaries = bootstrap._normalize_iteration_summaries(raw)
        assert len(summaries) == 1 and summaries[0]["summary"] == "done"
        feedback = bootstrap._normalize_feedback_history(raw)
        assert len(feedback) == 1 and "continue" in feedback[0]["text"]

        plot = bootstrap._plot_candidate_from_plot_entry(
            job_id, tmp_path, {"filename": "p.png", "title": "Plot"}
        )
        assert plot is not None and plot["file_path"] == "plots/p.png"
        assert bootstrap._plot_candidate_from_plot_entry(job_id, tmp_path, {}) is None
        ref = bootstrap._plot_candidate_from_finding_ref(job_id, 2, f"jobs/{job_id}/plots/a.png")
        assert ref is not None and ref["iteration"] == 2
        assert bootstrap._plot_candidate_from_finding_ref(job_id, 1, "   ") is None

    def test_rename_job_dir_conflict_and_oserror(self, tmp_path: Path) -> None:
        from openscientist import bootstrap

        result = bootstrap.BootstrapResult()
        job_dir = tmp_path / "legacy"
        job_dir.mkdir()
        target_id = uuid4()
        (tmp_path / str(target_id)).mkdir()
        assert (
            bootstrap._rename_job_dir(
                job_dir=job_dir, target_job_id=target_id, result=result, context="t"
            )
            is None
        )
        assert result.errors

        result2 = bootstrap.BootstrapResult()
        job_dir2 = tmp_path / "legacy2"
        job_dir2.mkdir()
        with patch.object(Path, "rename", side_effect=OSError("busy")):
            assert (
                bootstrap._rename_job_dir(
                    job_dir=job_dir2, target_job_id=uuid4(), result=result2, context="t"
                )
                is None
            )
        assert result2.errors

    def test_rewrite_paths_and_derive_updated_at(self) -> None:
        from datetime import UTC, datetime

        from openscientist import bootstrap

        rewritten = bootstrap._rewrite_legacy_path_prefixes(
            "jobs/old/plots/a.png", {"old"}, "new-id"
        )
        assert "new-id" in rewritten
        payload, changed = bootstrap._rewrite_payload_paths(
            {"path": "jobs/old/x.csv", "nested": ["jobs/old/y.csv"]},
            {"old"},
            "new-id",
        )
        assert changed is True
        assert "new-id" in payload["path"]

        created = datetime(2026, 1, 1, tzinfo=UTC)
        assert bootstrap._derive_updated_at({}, created) == created
        assert (
            bootstrap._derive_updated_at({"completed_at": "2026-02-01T00:00:00Z"}, created)
            is not None
        )


class TestPdfMarkdownHelpers:
    def test_handle_table_code_and_render_lines(self) -> None:
        from openscientist.pdf_generator import (
            _flush_table,
            _handle_fenced_code_line,
            _handle_table_line,
            _MarkdownRenderState,
            _render_markdown_line,
        )

        pdf = MagicMock()
        state = _MarkdownRenderState()
        assert _handle_fenced_code_line(pdf, "```python", state) is True
        assert state.in_code_block is True
        assert _handle_fenced_code_line(pdf, "```", state) is True
        pdf.add_code_block.assert_called()

        state = _MarkdownRenderState()
        assert _handle_table_line("| a | b |", state) is True
        assert _handle_table_line("|---|---|", state) is True
        assert _handle_table_line("not a table", state) is False
        _flush_table(pdf, state)
        pdf.add_table.assert_called_once()
        _flush_table(pdf, state)  # no-op

        state = _MarkdownRenderState()
        _render_markdown_line(pdf, "# Title", state)
        _render_markdown_line(pdf, "## H1", state)
        _render_markdown_line(pdf, "### H2", state)
        _render_markdown_line(pdf, "#### H3", state)
        _render_markdown_line(pdf, "- bullet", state)
        _render_markdown_line(pdf, "1. numbered", state)
        _render_markdown_line(pdf, "---", state)
        _render_markdown_line(pdf, "", state)
        _render_markdown_line(pdf, "paragraph", state)
        assert pdf.add_title.called
        assert pdf.add_paragraph.called


class TestSkillSyncSourcePaths:
    @pytest.mark.asyncio
    async def test_sync_source_rate_limit_success_and_error(self) -> None:
        from datetime import UTC, datetime, timedelta

        scheduler = SkillSyncScheduler(sync_interval=3600, github_token="t")
        source = cast(
            SkillSource,
            SimpleNamespace(
                id=uuid4(),
                name="src",
                last_synced_at=None,
                sync_error=None,
            ),
        )
        session = AsyncMock()
        scheduler._last_sync[str(source.id)] = datetime.now(UTC)
        assert await scheduler._sync_source_if_needed(session, source) is None

        scheduler._last_sync.clear()
        source.last_synced_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await scheduler._sync_source_if_needed(session, source) is None

        source.last_synced_at = None
        with patch(
            "openscientist.skill_scheduler.sync_skill_source",
            new_callable=AsyncMock,
            return_value={"created": 2, "updated": 1, "unchanged": 0, "errors": 0},
        ):
            ok = await scheduler._sync_source_if_needed(session, source, force=True)
        assert ok is not None and ok.success is True and ok.created == 2

        with patch(
            "openscientist.skill_scheduler.sync_skill_source",
            new_callable=AsyncMock,
            side_effect=RuntimeError("sync fail"),
        ):
            bad = await scheduler._sync_source_if_needed(session, source, force=True)
        assert bad is not None and bad.success is False
        assert bad.error_message == "sync fail"
        session.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_run_loop_initial_then_cancel(self) -> None:
        scheduler = SkillSyncScheduler(sync_interval=3600, github_token="t")
        scheduler._running = True
        with (
            patch.object(scheduler, "sync_all_sources", new_callable=AsyncMock) as sync,
            patch(
                "openscientist.skill_scheduler.asyncio.sleep",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
        ):
            await scheduler._run_loop()
        sync.assert_awaited()


class TestJobManagerCliAndNotify:
    def test_cli_list_get_delete_cleanup_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from openscientist import job_manager as jm

        job = JobInfo(
            job_id="abc",
            research_question="Q?",
            status=JobStatus.PENDING,
            created_at="2026-01-01T00:00:00+00:00",
            max_iterations=5,
            iterations_completed=1,
            findings_count=2,
        )
        manager = MagicMock()
        manager.list_jobs.return_value = [job]
        manager.get_job.side_effect = [None, job]
        manager.cleanup_old_jobs.return_value = 3
        manager.get_job_summary.return_value = {"total": 1}

        with (
            patch.object(sys, "argv", ["job_manager", "list"]),
            patch("openscientist.job.cli.JobManager", return_value=manager),
        ):
            jm.main()
        assert "abc" in capsys.readouterr().out

        with (
            patch.object(sys, "argv", ["job_manager", "get", "missing"]),
            patch("openscientist.job.cli.JobManager", return_value=manager),
        ):
            jm.main()
        assert "not found" in capsys.readouterr().out

        with (
            patch.object(sys, "argv", ["job_manager", "get", "abc"]),
            patch("openscientist.job.cli.JobManager", return_value=manager),
        ):
            jm.main()
        assert "abc" in capsys.readouterr().out

        with (
            patch.object(sys, "argv", ["job_manager", "delete", "abc"]),
            patch("openscientist.job.cli.JobManager", return_value=manager),
        ):
            jm.main()
        manager.delete_job.assert_called_with("abc")

        with (
            patch.object(
                sys, "argv", ["job_manager", "cleanup", "--days", "2", "--delete-completed"]
            ),
            patch("openscientist.job.cli.JobManager", return_value=manager),
        ):
            jm.main()
        assert "Deleted 3" in capsys.readouterr().out

        with (
            patch.object(sys, "argv", ["job_manager", "summary"]),
            patch("openscientist.job.cli.JobManager", return_value=manager),
        ):
            jm.main()
        assert "total" in capsys.readouterr().out

    def test_update_job_status_ntfy_success_and_failure(self, tmp_path: Path) -> None:
        from openscientist.job.types import JobStatusUpdateResult
        from openscientist.job_manager import JobManager

        manager = JobManager.__new__(JobManager)
        ntfy = JobStatusUpdateResult(
            ntfy_enabled=True,
            ntfy_topic="topic",
            owner_id=str(uuid4()),
            job_title="Research",
            current_iteration=3,
        )
        calls: list[object] = []

        def _run(coro: object) -> object:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            calls.append(coro)
            if len(calls) == 1:
                return ntfy
            return True

        with patch("openscientist.job_manager._run_async", side_effect=_run):
            manager._update_job_status("jid", JobStatus.COMPLETED)
        assert len(calls) == 2

        def _fail(coro: object) -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError("db down")

        with patch("openscientist.job_manager._run_async", side_effect=_fail):
            manager._update_job_status("jid", JobStatus.FAILED, error_message="x")

    def test_load_job_info_and_progress_helpers(self) -> None:
        from openscientist import job_manager as jm

        assert jm._derive_progress_from_db("running", 3) == 2
        assert jm._derive_progress_from_db("running", 1) == 0
        assert jm._derive_progress_from_db("completed", 4) == 4

        with patch(
            "openscientist.job_manager.KnowledgeState.load_from_database_sync",
            side_effect=RuntimeError("ks fail"),
        ):
            iters, findings = jm._load_progress_from_knowledge_state("j", "completed", 9, 8)
        assert (iters, findings) == (9, 8)

        manager = jm.JobManager.__new__(jm.JobManager)
        with patch(
            "openscientist.job_manager._run_async",
            side_effect=RuntimeError("load fail"),
        ):
            assert manager._load_job_info("missing") is None


class TestFoundryAndVertexCostSuccess:
    def test_foundry_get_cost_info_success(self) -> None:
        from openscientist.providers.foundry import FoundryProvider

        provider = object.__new__(FoundryProvider)
        settings = MagicMock()
        settings.provider.azure_subscription_id = "sub"
        settings.provider.azure_tenant_id = "tenant"
        settings.provider.azure_client_id = "cid"
        settings.provider.azure_client_secret = "sec"
        settings.provider.azure_resource_group = "rg"
        settings.provider.anthropic_foundry_resource = "res"
        fake_identity = MagicMock()
        fake_cm = MagicMock()
        with (
            patch("openscientist.providers.foundry.get_settings", return_value=settings),
            patch.dict(
                sys.modules,
                {
                    "azure.mgmt.costmanagement": fake_cm,
                    "azure.identity": fake_identity,
                },
            ),
            patch(
                "openscientist.providers.foundry._query_azure_cost_usd",
                side_effect=[10.0, 2.5],
            ),
        ):
            fake_cm.CostManagementClient = MagicMock(return_value=MagicMock())
            fake_identity.ClientSecretCredential = MagicMock(return_value=MagicMock())
            info = provider.get_cost_info(lookback_hours=12)
        assert info.total_spend_usd == 10.0
        assert info.recent_spend_usd == 2.5

    def test_foundry_get_cost_info_import_error(self) -> None:
        from openscientist.providers.foundry import FoundryProvider

        provider = object.__new__(FoundryProvider)
        settings = MagicMock()
        settings.provider.azure_subscription_id = "sub"
        real_import = builtins.__import__

        def _import(
            name: str,
            globals: Mapping[str, object] | None = None,
            locals: Mapping[str, object] | None = None,
            fromlist: Sequence[str] = (),
            level: int = 0,
        ) -> ModuleType:
            if name == "azure.mgmt.costmanagement" or name.startswith("azure.mgmt.costmanagement."):
                raise ImportError("missing sdk")
            return real_import(name, globals, locals, fromlist, level)

        with (
            patch("openscientist.providers.foundry.get_settings", return_value=settings),
            patch("builtins.__import__", side_effect=_import),
        ):
            info = provider.get_cost_info()
        assert info.total_spend_usd is None
        assert "azure-mgmt-costmanagement" in (info.data_lag_note or "")

    def test_vertex_get_cost_info_success_and_query_failure(self) -> None:
        from openscientist.providers.vertex import VertexProvider

        provider = object.__new__(VertexProvider)
        settings = MagicMock()
        settings.provider.google_application_credentials = "/tmp/creds.json"
        settings.provider.anthropic_vertex_project_id = "proj"
        settings.provider.gcp_billing_account_id = "billing-123"

        fake_bq = MagicMock()
        fake_sa = MagicMock()
        client = MagicMock()
        fake_bq.Client.return_value = client
        fake_sa.Credentials.from_service_account_file.return_value = MagicMock()

        row_total = SimpleNamespace(total_cost=5.0)
        row_recent = SimpleNamespace(recent_cost=1.5)
        client.query.side_effect = [
            MagicMock(result=MagicMock(return_value=iter([row_total]))),
            MagicMock(result=MagicMock(return_value=iter([row_recent]))),
        ]

        with (
            patch.dict(
                sys.modules,
                {
                    "google.cloud": MagicMock(bigquery=fake_bq),
                    "google.cloud.bigquery": fake_bq,
                    "google.oauth2": MagicMock(service_account=fake_sa),
                    "google.oauth2.service_account": fake_sa,
                },
            ),
            patch("openscientist.providers.vertex.get_settings", return_value=settings),
            patch("os.path.expanduser", return_value="/tmp/creds.json"),
        ):
            info = provider.get_cost_info(lookback_hours=6)
        assert info.total_spend_usd == 5.0
        assert info.recent_spend_usd == 1.5

        client.query.side_effect = RuntimeError("bq down")
        with (
            patch.dict(
                sys.modules,
                {
                    "google.cloud": MagicMock(bigquery=fake_bq),
                    "google.cloud.bigquery": fake_bq,
                    "google.oauth2": MagicMock(service_account=fake_sa),
                    "google.oauth2.service_account": fake_sa,
                },
            ),
            patch("openscientist.providers.vertex.get_settings", return_value=settings),
            patch("os.path.expanduser", return_value="/tmp/creds.json"),
        ):
            info2 = provider.get_cost_info()
        assert info2.total_spend_usd is None


class TestNtfyDbHelpers:
    @pytest.mark.asyncio
    async def test_get_user_ntfy_settings_paths(self) -> None:
        from openscientist import ntfy

        session = AsyncMock()
        row = SimpleNamespace(ntfy_enabled=True, ntfy_topic="t1")
        result = MagicMock()
        result.first.return_value = row
        session.execute = AsyncMock(return_value=result)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = session
        ctx.__aexit__.return_value = None
        with patch("openscientist.ntfy.AsyncSessionLocal", return_value=ctx):
            enabled, topic = await ntfy.get_user_ntfy_settings(uuid4())
        assert enabled is True and topic == "t1"

        result.first.return_value = None
        with patch("openscientist.ntfy.AsyncSessionLocal", return_value=ctx):
            enabled, topic = await ntfy.get_user_ntfy_settings(uuid4())
        assert enabled is False and topic is None

        with patch(
            "openscientist.ntfy.AsyncSessionLocal",
            side_effect=RuntimeError("db"),
        ):
            enabled, topic = await ntfy.get_user_ntfy_settings(uuid4())
        assert enabled is False and topic is None

    @pytest.mark.asyncio
    async def test_ensure_user_has_topic_paths(self) -> None:
        from openscientist import ntfy

        user = SimpleNamespace(ntfy_topic=None)
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        session.execute = AsyncMock(return_value=result)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = session
        ctx.__aexit__.return_value = None
        with patch("openscientist.ntfy.AsyncSessionLocal", return_value=ctx):
            topic = await ntfy.ensure_user_has_topic(uuid4())
        assert topic is not None and topic.startswith("openscientist-")
        session.commit.assert_awaited()

        result.scalar_one_or_none.return_value = None
        with patch("openscientist.ntfy.AsyncSessionLocal", return_value=ctx):
            assert await ntfy.ensure_user_has_topic(uuid4()) is None

        with patch(
            "openscientist.ntfy.AsyncSessionLocal",
            side_effect=RuntimeError("db"),
        ):
            assert await ntfy.ensure_user_has_topic(uuid4()) is None

    @pytest.mark.asyncio
    async def test_notify_job_status_change_branches(self) -> None:
        from openscientist import ntfy

        with (
            patch(
                "openscientist.ntfy.get_user_ntfy_settings",
                new_callable=AsyncMock,
                return_value=(False, None),
            ),
        ):
            assert await ntfy.notify_job_status_change(uuid4(), "j", "title", "running") is False

        with (
            patch(
                "openscientist.ntfy.get_user_ntfy_settings",
                new_callable=AsyncMock,
                return_value=(True, None),
            ),
            patch(
                "openscientist.ntfy.ensure_user_has_topic",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "openscientist.ntfy.get_settings",
                return_value=SimpleNamespace(base_url="http://localhost"),
            ),
        ):
            assert await ntfy.notify_job_status_change(uuid4(), "j", "title", "running") is False

        with (
            patch(
                "openscientist.ntfy.notify_job_started",
                new_callable=AsyncMock,
                return_value=True,
            ) as started,
            patch(
                "openscientist.ntfy.get_settings",
                return_value=SimpleNamespace(base_url="http://localhost"),
            ),
        ):
            assert (
                await ntfy.notify_job_status_change(
                    uuid4(), "j", "t" * 60, "running", ntfy_topic="topic"
                )
                is True
            )
            started.assert_awaited()

        for status, fn in [
            ("completed", "notify_job_completed"),
            ("failed", "notify_job_failed"),
            ("cancelled", "notify_job_cancelled"),
            ("awaiting_feedback", "notify_job_awaiting_feedback"),
        ]:
            with (
                patch(f"openscientist.ntfy.{fn}", new_callable=AsyncMock, return_value=True),
                patch(
                    "openscientist.ntfy.get_settings",
                    return_value=SimpleNamespace(base_url="http://localhost"),
                ),
            ):
                assert (
                    await ntfy.notify_job_status_change(
                        uuid4(), "j", "title", status, ntfy_topic="topic", iteration=2
                    )
                    is True
                )

        with patch(
            "openscientist.ntfy.get_settings",
            return_value=SimpleNamespace(base_url="http://localhost"),
        ):
            assert (
                await ntfy.notify_job_status_change(
                    uuid4(), "j", "title", "queued", ntfy_topic="topic"
                )
                is False
            )


class TestDiscoveryPdfAndFeedback:
    @pytest.mark.asyncio
    async def test_try_generate_report_pdf_success_and_double_fail(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import discovery

        report_path = tmp_path / "final_report.md"
        report_path.write_text("# Report\n", encoding="utf-8")

        with (
            patch(
                "openscientist.report.renderer.render_report_html",
                return_value="<html></html>",
            ),
            patch(
                "openscientist.report.pdf.render_report_pdf",
                new_callable=AsyncMock,
            ) as render_pdf,
        ):
            await discovery._try_generate_report_pdf(report_path)
            render_pdf.assert_awaited()

        with (
            patch(
                "openscientist.report.renderer.render_report_html",
                side_effect=RuntimeError("html fail"),
            ),
            patch(
                "openscientist.pdf_generator.markdown_to_pdf",
                side_effect=RuntimeError("pdf fail"),
            ),
            patch(
                "openscientist.report.processor.strip_figure_tags",
                side_effect=lambda text: text,
            ),
        ):
            await discovery._try_generate_report_pdf(report_path)

    @pytest.mark.asyncio
    async def test_wait_for_feedback_received(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import iteration as iteration_mod

        job_dir = tmp_path / str(uuid4())
        job_dir.mkdir()
        ks_initial = MagicMock()
        ks_initial.data = {"iteration": 2, "feedback_history": []}
        ks_updated = MagicMock()
        ks_updated.data = {
            "iteration": 2,
            "feedback_history": [{"after_iteration": 2, "text": "please dig deeper"}],
        }

        with (
            patch(
                "openscientist.orchestrator.iteration.KnowledgeState.load_from_database",
                new_callable=AsyncMock,
                side_effect=[ks_initial, ks_updated],
            ),
            patch(
                "openscientist.orchestrator.iteration.get_job_status",
                new_callable=AsyncMock,
                return_value="awaiting_feedback",
            ),
            patch("openscientist.orchestrator.iteration.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "openscientist.orchestrator.iteration.time.monotonic",
                side_effect=[0.0, 1.0],
            ),
        ):
            result = await iteration_mod.wait_for_feedback_or_timeout(job_dir, timeout_seconds=60)
        assert result["outcome"] == "feedback"
        assert result["feedback_text"] == "please dig deeper"


class TestReportProcessorEdges:
    def test_isolate_and_strip_figure_tags(self) -> None:
        from openscientist.report import processor

        md = "See {{figure:a.png|caption=Cap}} here"
        isolated = processor.isolate_figure_tags(md)
        assert "{{figure:a.png" in isolated
        table = "| a | {{figure:a.png|caption=Cap}} |\n"
        assert processor.isolate_figure_tags(table) == table
        fenced = "```\n{{figure:a.png}}\n```"
        assert "{{figure:a.png}}" in processor.isolate_figure_tags(fenced)
        assert "[Figure: Cap]" in processor.strip_figure_tags("{{figure:a.png|caption=Cap}}")
        assert processor._parse_params("") == {}
        assert processor._parse_params("|caption=X|width=1")["caption"] == "X"


class TestReportFigures:
    def test_build_figure_inventory_and_prompt(self, tmp_path: Path) -> None:
        from openscientist.report.figures import (
            FigureCard,
            build_figure_inventory,
            format_figure_inventory_prompt,
        )

        assert build_figure_inventory(tmp_path) == []
        assert format_figure_inventory_prompt([]) == ""

        prov = tmp_path / "provenance"
        prov.mkdir()
        (prov / "plot_a.png").write_bytes(b"png")
        (prov / "plot_a.json").write_text(
            '{"iteration": 1, "description": "A", "finding_ids": ["F1"]}',
            encoding="utf-8",
        )
        (prov / "orphan.png").write_bytes(b"png")
        (prov / "bad.json").write_text("{not-json", encoding="utf-8")
        (prov / "skip.json").write_text('{"other": true}', encoding="utf-8")
        (prov / "skip.png").write_bytes(b"png")

        cards = build_figure_inventory(tmp_path)
        names = {c.filename for c in cards}
        assert "plot_a.png" in names
        assert "orphan.png" in names
        prompt = format_figure_inventory_prompt(cards)
        assert "Available Figures" in prompt
        assert "plot_a.png" in prompt
        assert format_figure_inventory_prompt(
            [FigureCard(figure_id="x", filename="x.png", path=tmp_path / "x.png")]
        )


class TestGithubEmailHelpers:
    def test_email_selection_priority_and_fallback(self) -> None:
        from openscientist.auth.providers import github as gh

        emails = [
            {"email": "a@x.com", "primary": False, "verified": True},
            {"email": "b@x.com", "primary": True, "verified": True},
        ]
        assert gh._select_primary_verified_email(emails) == "b@x.com"
        assert gh._select_any_verified_email(emails) == "a@x.com"
        assert gh._select_primary_verified_email([]) is None

        profile_email, verified = gh._select_profile_email_with_verification(
            {"email": "b@x.com"}, emails
        )
        assert profile_email == "b@x.com" and verified is True
        assert gh._select_profile_email_with_verification({}, emails) == (None, False)

        fb_email, fb_ver = gh._select_fallback_email(
            [{"email": "c@x.com", "primary": True, "verified": False}]
        )
        assert fb_email == "c@x.com" and fb_ver is False
        assert gh._select_fallback_email([]) == (None, False)

        resolved, is_verified = gh._resolve_email_and_verification({"login": "user"}, [])
        assert resolved.endswith("@users.noreply.github.com")
        assert is_verified is False
        resolved2, _ = gh._resolve_email_and_verification({"login": "user"}, emails)
        assert resolved2 == "b@x.com"


class TestJobManagerFailureBranches:
    def test_budget_unavailable_and_exceeded(self, tmp_path: Path) -> None:
        from openscientist.exceptions import ProviderError
        from openscientist.job_manager import JobManager

        manager = JobManager.__new__(JobManager)
        provider = MagicMock()
        provider.check_budget_limits.side_effect = ProviderError("down")
        with patch("openscientist.job_manager.get_provider", return_value=provider):
            manager._check_budget_before_creation()

        provider.check_budget_limits.side_effect = None
        provider.check_budget_limits.return_value = {
            "can_proceed": False,
            "errors": ["over limit"],
        }
        with (
            patch("openscientist.job_manager.get_provider", return_value=provider),
            pytest.raises(ValueError, match="Cannot create job"),
        ):
            manager._check_budget_before_creation()

    def test_cleanup_old_jobs_skips_and_handles_errors(self, tmp_path: Path) -> None:
        from openscientist.job_manager import JobManager

        manager = JobManager.__new__(JobManager)
        manager.jobs_dir = tmp_path
        jobs = [
            JobInfo(
                job_id="run",
                research_question="q",
                status=JobStatus.RUNNING,
                created_at="2020-01-01T00:00:00",
            ),
            JobInfo(
                job_id="done",
                research_question="q",
                status=JobStatus.COMPLETED,
                created_at="2020-01-01T00:00:00",
            ),
            JobInfo(
                job_id="old",
                research_question="q",
                status=JobStatus.FAILED,
                created_at="2020-01-01T00:00:00",
            ),
        ]
        with (
            patch.object(manager, "_list_operational_jobs", return_value=jobs),
            patch.object(manager, "delete_job", side_effect=ValueError("nope")),
            patch("openscientist.job_manager.get_container_manager") as cm,
        ):
            cm.return_value.is_available.return_value = True
            cm.return_value.cleanup_orphaned_containers.return_value = 1
            deleted = manager.cleanup_old_jobs(days=7, keep_completed=True)
        assert deleted == 0

    def test_delete_job_container_cleanup_failure(self, tmp_path: Path) -> None:
        from openscientist.job_manager import JobManager

        manager = JobManager.__new__(JobManager)
        manager.jobs_dir = tmp_path
        job_id = str(uuid4())
        (tmp_path / job_id).mkdir()
        info = JobInfo(
            job_id=job_id,
            research_question="q",
            status=JobStatus.FAILED,
            created_at="2026-01-01T00:00:00+00:00",
        )

        def _run(coro: object) -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                close()

        with (
            patch.object(manager, "get_job", return_value=info),
            patch("openscientist.job_manager._run_async", side_effect=_run),
            patch(
                "openscientist.job_manager.get_container_manager",
                side_effect=RuntimeError("docker gone"),
            ),
        ):
            manager.delete_job(job_id)
        assert not (tmp_path / job_id).exists()

    def test_stale_cleanup_db_and_container_errors(self, tmp_path: Path) -> None:
        from openscientist.job_manager import JobManager

        manager = JobManager.__new__(JobManager)
        manager.jobs_dir = tmp_path
        stale = JobInfo(
            job_id=str(uuid4()),
            research_question="q",
            status=JobStatus.RUNNING,
            created_at="2026-01-01T00:00:00+00:00",
            owner_id=str(uuid4()),
        )

        def _run(coro: object) -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError("db")

        with (
            patch.object(manager, "_list_operational_jobs", return_value=[stale]),
            patch("openscientist.job_manager._run_async", side_effect=_run),
            patch(
                "openscientist.job_container.JobContainerRunner",
                side_effect=RuntimeError("cleanup fail"),
            ),
        ):
            manager._cleanup_stale_jobs()


class TestSkillIngestionUnknownType:
    @pytest.mark.asyncio
    async def test_sync_skill_source_unknown_type(self) -> None:
        from openscientist.skill_ingestion import sync_skill_source

        source = cast(SkillSource, SimpleNamespace(source_type="unknown-type"))
        with pytest.raises(ValueError, match="Unknown source type"):
            await sync_skill_source(AsyncMock(), source)


class TestPricingAndBadgeHelpers:
    def test_pricing_fetch_failure_uses_fallback(self) -> None:
        from openscientist.providers import pricing as pricing_mod

        pricing_mod._cache = {}
        pricing_mod._cache_fetched_at = 0.0
        with patch("openscientist.providers.pricing.requests.get", side_effect=RuntimeError("net")):
            data = pricing_mod._get_litellm_pricing()
        assert isinstance(data, dict)
        assert "claude" in pricing_mod.normalize_model_name(
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )
        assert pricing_mod.estimate_cost_usd("missing-model", 10, 10) == 0.0

    def test_status_badge_props_and_slot(self) -> None:
        from openscientist.webapp_components.components.badges import (
            get_status_badge_props,
            render_status_cell_slot,
        )

        failed = get_status_badge_props(JobStatus.FAILED)
        assert failed["classes"]
        assert get_status_badge_props(JobStatus.COMPLETED)["color"]
        slot = render_status_cell_slot()
        assert "failed" in slot and "completed" in slot

    def test_project_resource_links(self) -> None:
        from openscientist.webapp_components.ui_components import get_project_resource_links

        links = get_project_resource_links()
        assert any(label == "GitHub" for label, _ in links)


class TestProviderSendMessageWithTools:
    @pytest.mark.asyncio
    async def test_anthropic_and_cborg_tool_paths(self) -> None:
        from openscientist.providers.anthropic import AnthropicProvider
        from openscientist.providers.cborg import CborgProvider

        settings = MagicMock()
        settings.provider.anthropic_api_key = "sk"
        settings.provider.anthropic_auth_token = "tok"
        settings.provider.anthropic_base_url = "http://cborg"
        settings.provider.model = "m"

        anthropic_provider = object.__new__(AnthropicProvider)
        with (
            patch("openscientist.providers.anthropic.get_settings", return_value=settings),
            patch("anthropic.Anthropic"),
            patch(
                "openscientist.providers.anthropic.send_anthropic_message_with_tools",
                return_value={"ok": True},
            ),
        ):
            result = await anthropic_provider.send_message_with_tools(
                [{"role": "user", "content": "hi"}],
                tools=[{"name": "t"}],
            )
        assert result == {"ok": True}

        cborg = object.__new__(CborgProvider)
        with (
            patch("openscientist.providers.cborg.get_settings", return_value=settings),
            patch("anthropic.Anthropic"),
            patch(
                "openscientist.providers.cborg.send_anthropic_message",
                return_value="hi",
            ),
            patch(
                "openscientist.providers.cborg.send_anthropic_message_with_tools",
                return_value={"ok": True},
            ),
        ):
            assert await cborg.send_message([{"role": "user", "content": "hi"}]) == "hi"
            assert (
                await cborg.send_message_with_tools([{"role": "user", "content": "hi"}], tools=[])
            ) == {"ok": True}


class TestGithubUserInfoAndPubMed:
    @pytest.mark.asyncio
    async def test_github_get_user_info_success_and_errors(self) -> None:
        from openscientist.auth.providers.github import GitHubProvider

        with pytest.raises(ValueError, match="access_token"):
            await GitHubProvider.get_user_info({})

        user_resp = MagicMock()
        user_resp.raise_for_status = MagicMock()
        user_resp.json.return_value = {
            "id": 1,
            "login": "octocat",
            "name": "Octo",
            "email": "o@x.com",
        }
        emails_resp = MagicMock()
        emails_resp.raise_for_status = MagicMock()
        emails_resp.json.return_value = [
            {"email": "o@x.com", "primary": True, "verified": True},
        ]

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[user_resp, emails_resp])
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None

        with patch("openscientist.auth.providers.github.httpx.AsyncClient", return_value=client):
            info = await GitHubProvider.get_user_info({"access_token": "tok"})
        assert info["username"] == "octocat"
        assert info["email_verified"] is True

        import httpx

        client.get = AsyncMock(side_effect=[user_resp, httpx.HTTPError("emails down")])
        with patch("openscientist.auth.providers.github.httpx.AsyncClient", return_value=client):
            info2 = await GitHubProvider.get_user_info({"access_token": "tok"})
        assert info2["username"] == "octocat"

    def test_pubmed_badge_html(self) -> None:
        from openscientist.webapp_components.components.badges import (
            _get_pubmed_badge_html,
            transform_pmid_references,
        )

        html = _get_pubmed_badge_html("12345678")
        assert "12345678" in html
        assert "pubmed.ncbi.nlm.nih.gov" in html
        assert transform_pmid_references("") == ""
        transformed = transform_pmid_references("See PMID: 12345678 and PMID 87654321")
        assert "pubmed-badge" in transformed


class TestSkillSyncByIdAndRls:
    @pytest.mark.asyncio
    async def test_sync_source_by_id_invalid_and_missing(self) -> None:
        scheduler = SkillSyncScheduler(sync_interval=3600, github_token="t")
        assert await scheduler.sync_source_by_id("not-a-uuid") is None

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = session
        ctx.__aexit__.return_value = None
        with patch("openscientist.skill_scheduler.get_admin_session", return_value=ctx):
            assert await scheduler.sync_source_by_id(str(uuid4())) is None

    @pytest.mark.asyncio
    async def test_apply_rls_and_share_permission(self) -> None:
        from openscientist import job_manager as jm

        session = AsyncMock()
        await jm._apply_rls_context(session, None)
        session.execute.assert_not_called()

        with patch(
            "openscientist.job_manager.set_current_user", new_callable=AsyncMock
        ) as set_user:
            await jm._apply_rls_context(session, uuid4())
            set_user.assert_awaited()

        result = MagicMock()
        result.scalar_one_or_none.return_value = "view"
        session.execute = AsyncMock(return_value=result)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = session
        ctx.__aexit__.return_value = None
        with (
            patch("openscientist.job_manager.AsyncSessionLocal", return_value=ctx),
            patch("openscientist.job_manager._apply_rls_context", new_callable=AsyncMock),
        ):
            perm = await jm._db_get_share_permission(str(uuid4()), uuid4())
        assert perm == "view"


class TestGoogleProviderUserInfo:
    @pytest.mark.asyncio
    async def test_google_get_user_info(self) -> None:
        from openscientist.auth.providers.google import GoogleProvider

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "sub": "gid",
            "email": "g@x.com",
            "name": "G",
            "email_verified": True,
        }
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        with patch("openscientist.auth.providers.google.httpx.AsyncClient", return_value=client):
            info = await GoogleProvider.get_user_info({"access_token": "tok"})
        assert info["email"] == "g@x.com"

        with pytest.raises(ValueError):
            await GoogleProvider.get_user_info({})

        resp.json.return_value = {"sub": "gid2"}
        with patch("openscientist.auth.providers.google.httpx.AsyncClient", return_value=client):
            info2 = await GoogleProvider.get_user_info({"access_token": "tok"})
        assert info2["email"].endswith("@google.invalid")


class TestSkillSyncRegisteredIngester:
    @pytest.mark.asyncio
    async def test_sync_skill_source_dispatches_registered_ingester(self) -> None:
        from openscientist import skill_ingestion as si

        class _FakeIngester:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def sync_source(
                self, session: object, source: object, **kwargs: object
            ) -> dict[str, int]:
                return {"created": 1, "updated": 0, "unchanged": 0, "errors": 0}

            async def close(self) -> None:
                return None

        si.register_ingester("r19-test-type", _FakeIngester)  # type: ignore[arg-type]
        source = cast(SkillSource, SimpleNamespace(source_type="r19-test-type"))
        stats = await si.sync_skill_source(AsyncMock(), source)
        assert stats["created"] == 1
        # non-github branch
        si.register_ingester("r19-other", _FakeIngester)  # type: ignore[arg-type]
        source2 = cast(SkillSource, SimpleNamespace(source_type="r19-other"))
        assert (await si.sync_skill_source(AsyncMock(), source2))["created"] == 1


class TestDiscoveryPersistAndLoad:
    @pytest.mark.asyncio
    async def test_persist_final_status_completed_and_failed(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import discovery

        job_dir = tmp_path / str(uuid4())
        job_dir.mkdir()
        with patch(
            "openscientist.orchestrator.discovery.update_job_status",
            new_callable=AsyncMock,
        ) as upd:
            ok = await discovery._persist_final_status(
                job_dir, discovery._ReportOutcome(success=True, error="")
            )
            bad = await discovery._persist_final_status(
                job_dir, discovery._ReportOutcome(success=False, error="boom")
            )
        assert ok == "completed"
        assert bad == "failed"
        assert upd.await_count == 2

    @pytest.mark.asyncio
    async def test_load_runtime_context_job_missing(self, tmp_path: Path) -> None:
        from openscientist.orchestrator import discovery

        job_dir = tmp_path / str(uuid4())
        job_dir.mkdir()
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        ctx = AsyncMock()
        ctx.__aenter__.return_value = session
        ctx.__aexit__.return_value = None
        with (
            patch("openscientist.orchestrator.discovery.AsyncSessionLocal", return_value=ctx),
            pytest.raises(ValueError, match="not found"),
        ):
            await discovery._load_runtime_context(job_dir)


class TestAuthMiddlewareHelpers:
    def test_store_clear_and_redirect_helpers(self) -> None:
        from openscientist.auth import middleware as mw

        storage: dict[str, object] = {}
        fake_app = SimpleNamespace(storage=SimpleNamespace(user=storage))
        with (
            patch.object(mw, "app", fake_app),
            patch.object(
                mw,
                "ui",
                SimpleNamespace(navigate=SimpleNamespace(to=MagicMock()), context=MagicMock()),
            ),
        ):
            mw._store_authenticated_user(
                "tok",
                {
                    "user_id": "u1",
                    "email": "e@x.com",
                    "name": "N",
                    "is_admin": True,
                    "is_approved": True,
                    "can_start_jobs": True,
                },
            )
            assert storage["authenticated"] is True
            mw._clear_user_storage(tolerate_uninitialized=True)
            mw._redirect_to_login(clear_storage=True, tolerate_uninitialized_storage=True)

    @pytest.mark.asyncio
    async def test_require_auth_async_redirects_without_token(self) -> None:
        from openscientist.auth.middleware import require_auth

        @require_auth
        async def _page() -> str:
            return "ok"

        with (
            patch("openscientist.auth.middleware._get_session_token", return_value=None),
            patch("openscientist.auth.middleware._redirect_to_login") as redirect,
        ):
            assert await _page() is None
            redirect.assert_called()


class TestUiComponentPureRenders:
    def test_render_helpers_with_mocked_ui(self) -> None:
        from openscientist.webapp_components import ui_components as uic

        class _Ctx:
            def __enter__(self) -> "_Ctx":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def classes(self, *_a: object, **_k: object) -> "_Ctx":
                return self

        mock_ui = MagicMock()
        mock_ui.column.return_value = _Ctx()
        mock_ui.row.return_value = _Ctx()
        mock_ui.card.return_value = _Ctx()
        mock_ui.label.return_value = MagicMock(classes=MagicMock(return_value=MagicMock()))
        mock_ui.link.return_value = MagicMock(classes=MagicMock(return_value=MagicMock()))
        mock_ui.button.return_value = MagicMock(props=MagicMock(return_value=_Ctx()))
        mock_ui.spinner.return_value = MagicMock()
        mock_ui.icon.return_value = MagicMock(classes=MagicMock(return_value=MagicMock()))
        html_el = MagicMock()
        html_el.classes.return_value = html_el
        html_el.style.return_value = html_el
        mock_ui.html.return_value = html_el
        mock_ui.tooltip = MagicMock()
        mock_ui.navigate = SimpleNamespace(to=MagicMock())
        mock_ui.add_head_html = MagicMock()

        with patch.object(uic, "ui", mock_ui):
            uic.render_project_resource_links()
            uic.render_empty_state("nothing here")
            uic.render_pending_approval_notice()
            uic.render_loading_spinner("wait")
            uic.render_job_action_buttons(
                on_share=lambda: None,
                on_delete=lambda: None,
                on_notifications=lambda: None,
            )
            uic.render_not_found_state("missing", "gone", back_url="/")
            uic.render_error_state("err", "bad", back_url="/")
            uic._inject_thinking_status_styles()
            uic.render_thinking_status("Working...")

    def test_pmid_text_render_with_mocked_ui(self) -> None:
        from openscientist.webapp_components.components import badges

        mock_ui = MagicMock()
        mock_ui.html = MagicMock()
        mock_ui.add_head_html = MagicMock()
        with patch.object(badges, "ui", mock_ui):
            badges.render_pmid_badge("12345")
            badges.render_text_with_pmid_links("")
            badges.render_text_with_pmid_links("No ids here")
            badges.render_text_with_pmid_links("See PMID: 12345678 for details")
        assert mock_ui.html.called

"""Tests for job-detail action gating (admin "Regenerate report")."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages import job_detail


def _context(status: JobStatus) -> SimpleNamespace:
    """Minimal stand-in carrying only what _can_regenerate_report reads."""
    return SimpleNamespace(job_info=SimpleNamespace(status=status))


def _action_context() -> SimpleNamespace:
    """Stand-in for the regenerate action handler with a recording manager."""
    return SimpleNamespace(
        job_id="job-1",
        job_manager=SimpleNamespace(regenerate_report=AsyncMock()),
    )


@pytest.mark.parametrize(
    "is_admin,status,expected",
    [
        (True, JobStatus.COMPLETED, True),  # admin + completed -> shown
        (False, JobStatus.COMPLETED, False),  # non-admin -> hidden
        (True, JobStatus.RUNNING, False),  # not completed -> hidden
        (True, JobStatus.FAILED, False),  # not completed -> hidden
        (False, JobStatus.RUNNING, False),  # neither -> hidden
    ],
)
def test_can_regenerate_report_gating(is_admin: bool, status: JobStatus, expected: bool) -> None:
    with patch.object(job_detail, "is_current_user_admin", return_value=is_admin):
        assert job_detail._can_regenerate_report(_context(status)) is expected  # type: ignore[arg-type]


async def test_regenerate_action_blocks_non_admin_server_side() -> None:
    """The action handler re-checks admin and never reaches the manager for a
    non-admin, even if the button were somehow triggered."""
    context = _action_context()
    with (
        patch.object(job_detail, "is_current_user_admin", return_value=False),
        patch.object(job_detail, "ui"),  # swallow notify/navigate
    ):
        await job_detail._regenerate_report(context)  # type: ignore[arg-type]
    context.job_manager.regenerate_report.assert_not_called()


async def test_regenerate_action_calls_manager_for_admin() -> None:
    """An admin action reaches the manager with the job id."""
    context = _action_context()
    with (
        patch.object(job_detail, "is_current_user_admin", return_value=True),
        patch.object(job_detail, "ui"),  # swallow notify/navigate
    ):
        await job_detail._regenerate_report(context)  # type: ignore[arg-type]
    context.job_manager.regenerate_report.assert_awaited_once_with("job-1")


class TestTimelineHeaderText:
    """The per-iteration timeline header must not mislabel a summary-less
    iteration as 'Completed' (which reads as the whole job being done)."""

    def test_strapline_wins(self) -> None:
        assert job_detail._timeline_header_text("Found X", "long summary", False) == "Found X"

    def test_summary_used_when_no_strapline(self) -> None:
        assert job_detail._timeline_header_text("", "Did Y", False) == "Did Y"

    def test_in_progress_without_summary(self) -> None:
        assert job_detail._timeline_header_text("", "", True) == "Investigation in progress..."

    def test_no_summary_no_activity_is_not_labelled_completed(self) -> None:
        # The regression: a finished iteration with no summary used to say
        # "Completed". With no activity either, say so plainly.
        out = job_detail._timeline_header_text("", "", False, has_activity=False)
        assert out == "No activity or summary recorded"
        assert out != "Completed"

    def test_activity_without_summary_is_distinguished(self) -> None:
        # An iteration that did work but skipped save_iteration_summary must not
        # read as idle (and must not say "Completed").
        out = job_detail._timeline_header_text("", "", False, has_activity=True)
        assert out == "Activity logged, but no summary recorded"
        assert out != "Completed"

    def test_in_progress_suffix_on_strapline(self) -> None:
        assert job_detail._timeline_header_text("Found X", "", True) == "Found X [in progress]"


class TestDownloadArtifactsZipStreamsOverHttp:
    def test_triggers_http_download_of_session_route(self) -> None:
        with patch.object(job_detail, "ui") as mock_ui:
            job_detail._download_artifacts_zip("job-1")

        mock_ui.download.assert_called_once_with("/web/jobs/job-1/artifacts.zip")


class TestDownloadPdfReport:
    """The report PDF is rendered off the event loop and handed to the browser."""

    async def test_pdf_render_runs_off_the_loop_and_downloads_the_file(self, tmp_path) -> None:
        report_path = tmp_path / "final_report.md"
        report_path.write_text("# Report\n\nSome text", encoding="utf-8")
        pdf_path = tmp_path / "final_report.pdf"

        loop_thread = threading.get_ident()
        render_threads: list[int] = []

        footers: list[str | None] = []

        def fake_markdown_to_pdf(source, target, run_environment=None) -> None:
            render_threads.append(threading.get_ident())
            footers.append(run_environment)
            target.write_bytes(b"PDF-CONTENT")

        with (
            patch.object(job_detail, "ui") as mock_ui,
            patch.object(job_detail, "markdown_to_pdf", side_effect=fake_markdown_to_pdf),
        ):
            await job_detail._download_pdf_report(
                report_path, pdf_path, "job-9", "Run environment: OpenScientist abc123"
            )

        mock_ui.download.assert_called_once_with(b"PDF-CONTENT", filename="job-9_report.pdf")
        assert not (tmp_path / "_final_report_stripped.md").exists()
        assert footers == ["Run environment: OpenScientist abc123"]
        # Rendering a long report takes seconds of CPU, so it must not run on the loop thread.
        assert render_threads and render_threads[0] != loop_thread


class TestRegenerateConfirmDialog:
    """The confirmation dialog's Regenerate button runs the admin action."""

    async def test_confirm_closes_the_dialog_and_launches_the_action(self) -> None:
        context = SimpleNamespace(
            job_id="job-1",
            job_info=SimpleNamespace(status=JobStatus.COMPLETED),
            job_manager=SimpleNamespace(regenerate_report=AsyncMock()),
        )

        def _ctx_factory():
            element = MagicMock()
            element.__enter__ = MagicMock(return_value=element)
            element.__exit__ = MagicMock(return_value=False)
            element.classes.return_value = element
            return element

        mock_ui = MagicMock()
        for name in ("card", "row", "column"):
            setattr(mock_ui, name, _ctx_factory)

        with (
            patch.object(job_detail, "ui", mock_ui),
            patch.object(job_detail, "is_current_user_admin", return_value=True),
        ):
            job_detail._confirm_regenerate_report(context)  # type: ignore[arg-type]
            regenerate = [
                c for c in mock_ui.button.call_args_list if c.args and c.args[0] == "Regenerate"
            ][0]
            await regenerate.kwargs["on_click"]()

        context.job_manager.regenerate_report.assert_awaited_once_with("job-1")  # type: ignore[attr-defined]
        mock_ui.notify.assert_called_with(
            "Regenerating report. This page will update when it finishes.", type="positive"
        )
        mock_ui.navigate.to.assert_called_once_with("/job/job-1")

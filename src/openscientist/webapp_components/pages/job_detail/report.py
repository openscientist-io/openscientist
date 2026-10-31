"""Report tab: downloads, regeneration and the report view."""

import asyncio
import logging
from pathlib import Path

from nicegui import ui

from openscientist.auth import is_current_user_admin
from openscientist.job.types import JobStatus
from openscientist.pdf_generator import markdown_to_pdf
from openscientist.run_environment import run_environment_text
from openscientist.webapp_components.pages.job_detail.common import _JobDetailContext
from openscientist.webapp_components.ui_components import (
    render_thinking_status,
    transform_pmid_references,
)

logger = logging.getLogger(__name__)


def _download_artifacts_zip(job_id: str) -> None:
    ui.download(f"/web/jobs/{job_id}/artifacts.zip")


async def _download_pdf_report(
    report_path: Path, pdf_path: Path, job_id: str, run_environment: str | None
) -> None:
    # Serve existing PDF if available (avoids overwriting WeasyPrint PDF with fpdf2)
    if pdf_path.exists():
        ui.download(pdf_path.read_bytes(), filename=f"{job_id}_report.pdf")
        return
    # Fallback: generate via fpdf2
    try:
        from openscientist.report.processor import strip_figure_tags

        raw_md = report_path.read_text(encoding="utf-8")
        stripped = strip_figure_tags(raw_md)
        stripped_path = report_path.parent / "_final_report_stripped.md"
        stripped_path.write_text(stripped, encoding="utf-8")
        try:
            # Rendering a long report takes seconds of CPU.
            await asyncio.to_thread(
                markdown_to_pdf, stripped_path, pdf_path, run_environment=run_environment
            )
        finally:
            stripped_path.unlink(missing_ok=True)
        ui.download(pdf_path.read_bytes(), filename=f"{job_id}_report.pdf")
    except Exception as exc:
        logger.error("PDF generation failed: %s", exc, exc_info=True)
        ui.notify("Failed to generate PDF. Please try again.", type="negative")


def _can_regenerate_report(context: _JobDetailContext) -> bool:
    """Whether the admin "Regenerate report" control should be shown.

    Admin-only, and only for completed jobs (the report phase reuses the
    persisted findings, which only exist once the job has finished).
    """
    return is_current_user_admin() and context.job_info.status == JobStatus.COMPLETED


async def _regenerate_report(context: _JobDetailContext) -> None:
    """Admin action: re-run only the report-generation phase for this job.

    Launches the agent container in report-only mode (the discovery iterations
    are not re-run; the persisted findings are reused). Overwrites the existing
    final report, so the caller confirms first.
    """
    # Server-side authorization: never rely on the button only being rendered
    # for admins. is_current_user_admin() reads the auth-time is_admin flag in
    # app.storage.user (set during authentication, not forgeable by the client
    # over the websocket), so re-checking it here guards the action itself.
    if not is_current_user_admin():
        logger.warning("Non-admin attempt to regenerate report for job %s blocked", context.job_id)
        ui.notify("You are not authorized to regenerate reports.", type="negative")
        return
    try:
        await context.job_manager.regenerate_report(context.job_id)
    except ValueError as exc:
        ui.notify(str(exc), type="negative")
        return
    except Exception:
        logger.exception("Failed to start report regeneration for job %s", context.job_id)
        ui.notify("Failed to start report regeneration. Please try again.", type="negative")
        return
    ui.notify("Regenerating report. This page will update when it finishes.", type="positive")
    ui.navigate.to(f"/job/{context.job_id}")


def _confirm_regenerate_report(context: _JobDetailContext) -> None:
    """Confirm before overwriting the existing report."""
    with ui.dialog() as dialog, ui.card():
        ui.label("Regenerate report?").classes("text-lg font-bold")
        ui.label(
            "This re-runs only the report-generation step using the existing "
            "findings and overwrites the current report. The investigation "
            "iterations are not re-run."
        ).classes("text-sm text-gray-600")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat color=grey")

            async def _confirm() -> None:
                dialog.close()
                await _regenerate_report(context)

            ui.button("Regenerate", on_click=_confirm).props("color=primary")
    dialog.open()


def _render_report_actions(context: _JobDetailContext, report_path: Path, pdf_path: Path) -> None:
    with ui.row().classes("w-full justify-end mb-4 gap-2"):
        if pdf_path.exists() or report_path.exists():
            ui.button(
                "Download PDF",
                on_click=lambda: _download_pdf_report(
                    report_path,
                    pdf_path,
                    context.job_id,
                    run_environment_text(context.job_info.version_info),
                ),
                icon="picture_as_pdf",
            ).props("color=primary")
        else:
            ui.button("PDF Unavailable", icon="picture_as_pdf").props("color=grey outline disabled")

        ui.button(
            "Download All Artifacts",
            on_click=lambda: _download_artifacts_zip(context.job_id),
            icon="folder_zip",
        ).props("color=accent outline")

        # Admin-only: re-run just the report phase against the persisted
        # findings. Visible only to admins and only for completed jobs.
        if _can_regenerate_report(context):
            with ui.button(
                "Regenerate Report",
                on_click=lambda: _confirm_regenerate_report(context),
                icon="refresh",
            ).props("color=warning outline"):
                ui.tooltip("Admin: re-run the report step using existing findings")


def _render_report_html_iframe(job_dir: Path) -> None:
    """Render HTML report in an iframe to avoid CSS leakage."""
    if not (job_dir / "final_report.html").exists():
        return
    ui.element("iframe").props(f'src="/web/jobs/{job_dir.name}/report.html" frameborder="0"').style(
        "width: 100%; height: 80vh; border: 1px solid #ddd; border-radius: 8px;"
    )


def _render_report_markdown(report_path: Path) -> None:
    with open(report_path, encoding="utf-8") as report_file:
        report_content = report_file.read()
    ui.markdown(transform_pmid_references(report_content)).classes("w-full")


def _render_missing_report_state(context: _JobDetailContext) -> None:
    if context.job_info.status == JobStatus.GENERATING_REPORT:
        ui.label("Report is being generated...").classes("text-gray-500 italic")
        return
    if context.job_info.status in [JobStatus.COMPLETED, JobStatus.FAILED]:
        ui.label("Report generation failed").classes("text-red-500")
        return
    ui.label("Report will be available when job completes").classes("text-gray-500 italic")


def _render_report_tab(context: _JobDetailContext) -> None:
    report_path = context.job_dir / "final_report.md"
    html_path = context.job_dir / "final_report.html"
    pdf_path = context.job_dir / "final_report.pdf"

    if context.job_info.status == JobStatus.GENERATING_REPORT:
        render_thinking_status("Generating report...")

    # Only show the report when the job has finished. The agent may write
    # final_report.md mid-run, but it is not ready for display until the
    # orchestrator marks the job completed (or failed).
    if context.job_info.status not in (JobStatus.COMPLETED, JobStatus.FAILED):
        _render_missing_report_state(context)
        return

    if report_path.exists():
        _render_report_actions(context, report_path, pdf_path)
        # Prefer HTML report (with embedded figures) over raw markdown
        if html_path.exists():
            _render_report_html_iframe(context.job_dir)
        else:
            _render_report_markdown(report_path)
        return

    _render_missing_report_state(context)

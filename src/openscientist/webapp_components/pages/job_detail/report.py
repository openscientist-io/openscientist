"""Report tab: downloads, regeneration and the report view."""

import asyncio
import logging
import tempfile
from pathlib import Path

from nicegui import ui
from nicegui.elements.markdown import prepare_content
from nicegui.elements.tabs import Tab, TabPanel
from nicegui.events import ValueChangeEventArguments

from openscientist.artifact_packager import MAX_ARTIFACT_FILE_SIZE_BYTES
from openscientist.auth import is_current_user_admin
from openscientist.job.types import JobStatus
from openscientist.job_files import (
    install_file,
    job_file_exists,
    read_job_file_bytes,
    read_job_file_text,
    try_open_job_file,
)
from openscientist.pdf_generator import markdown_to_pdf
from openscientist.run_environment import run_environment_text
from openscientist.webapp_components.components.badges import transform_pmid_references
from openscientist.webapp_components.pages.job_detail.common import _JobDetailContext
from openscientist.webapp_components.utils.permissions import revalidate_admin_session

logger = logging.getLogger(__name__)


def _download_artifacts_zip(job_id: str) -> None:
    ui.download(f"/web/jobs/{job_id}/artifacts.zip")


async def _download_pdf_report(
    report_path: Path, pdf_path: Path, job_id: str, run_environment: str | None
) -> None:
    job_dir = report_path.parent
    # The job directory is agent-writable, so the report files are read and
    # installed through the symlink-safe walk, and the PDF renders outside
    # the job directory before it is moved into place.
    existing = read_job_file_bytes(job_dir, pdf_path.name, max_size=MAX_ARTIFACT_FILE_SIZE_BYTES)
    if existing is not None:
        # Serve existing PDF if available (avoids overwriting WeasyPrint PDF
        # with fpdf2)
        ui.download(existing, filename=f"{job_id}_report.pdf")
        return
    if job_file_exists(job_dir, pdf_path.name):
        ui.notify("The report is too large to download.", type="warning")
        return
    # Fallback: generate via fpdf2
    raw_md = read_job_file_text(job_dir, report_path.name, max_size=MAX_ARTIFACT_FILE_SIZE_BYTES)
    if raw_md is None:
        if job_file_exists(job_dir, report_path.name):
            ui.notify("The report is too large to download.", type="warning")
            return
        logger.error("PDF generation failed: %s", "no readable final_report.md")
        ui.notify("Failed to generate PDF. Please try again.", type="negative")
        return
    try:
        from openscientist.report.processor import strip_figure_tags

        with tempfile.TemporaryDirectory(prefix=f"openscientist_{job_id}_") as tmp_dir:
            stripped_path = Path(tmp_dir) / "_final_report_stripped.md"
            stripped_path.write_text(strip_figure_tags(raw_md), encoding="utf-8")
            generated_pdf = Path(tmp_dir) / pdf_path.name
            # Rendering a long report takes seconds of CPU.
            await asyncio.to_thread(
                markdown_to_pdf, stripped_path, generated_pdf, run_environment=run_environment
            )
            install_file(job_dir, pdf_path.name, generated_pdf)
        generated = read_job_file_bytes(
            job_dir, pdf_path.name, max_size=MAX_ARTIFACT_FILE_SIZE_BYTES
        )
        if generated is None:
            raise OSError("final_report.pdf is not a readable regular file")
        ui.download(generated, filename=f"{job_id}_report.pdf")
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
    # Server-side authorization: the caller's session and admin flag are
    # re-read from the database at action time, so an admin who was logged
    # out, demoted or deleted loses the action in an open tab.
    if not await revalidate_admin_session():
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
        if job_file_exists(context.job_dir, pdf_path.name) or job_file_exists(
            context.job_dir, report_path.name
        ):
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


def _report_html_available(job_dir: Path) -> bool:
    """Whether the report route can serve the job's HTML report, which it
    refuses when the file exceeds the size cap, so a too large file falls
    back to the markdown view rather than a frame that 404s."""
    job_file = try_open_job_file(
        job_dir, "final_report.html", max_size=MAX_ARTIFACT_FILE_SIZE_BYTES
    )
    if job_file is None:
        return False
    job_file.close()
    return True


def _render_report_html_iframe(job_dir: Path, tabs: ui.tabs, report_tab: ui.tab) -> None:
    """Render HTML report in an iframe to avoid CSS leakage.

    The frame stays in the page across tab switches, so it gets its source
    when the Report tab first opens and keeps it, and a page that never shows
    the report never has the route render it.
    """
    if not _report_html_available(job_dir):
        return
    frame = (
        ui.element("iframe")
        .props('frameborder="0"')
        .style("width: 100%; height: 80vh; border: 1px solid #ddd; border-radius: 8px;")
    )

    def _load_on_first_visit(e: ValueChangeEventArguments[str | Tab | TabPanel | None]) -> None:
        if e.value == report_tab.props["name"] and "src" not in frame.props:
            frame.props(f'src="/web/jobs/{job_dir.name}/report.html"')

    tabs.on_value_change(_load_on_first_visit)


def _transformed_report_markdown(report_path: Path) -> str | None:
    """The report's markdown with PMID links transformed, its HTML conversion
    already computed.

    The conversion is the one NiceGUI's markdown element performs at creation,
    so precomputing it warms the content cache that the element call then
    consults, and a large report keeps the event loop free.
    """
    report_content = read_job_file_text(
        report_path.parent, report_path.name, max_size=MAX_ARTIFACT_FILE_SIZE_BYTES
    )
    if report_content is None:
        return None
    content = transform_pmid_references(report_content)
    prepare_content(content, extras=" ".join(ui.markdown.default_extras))
    return content


async def _render_report_markdown(container: ui.column, report_path: Path) -> None:
    """Convert the markdown off the event loop and render it into ``container``.

    The conversion warms NiceGUI's markdown content cache, so creating the
    element stays cheap. A client that disconnected during the conversion is
    skipped.
    """
    content = await asyncio.to_thread(_transformed_report_markdown, report_path)
    if container.is_deleted:
        return
    container.clear()
    if content is None:
        if job_file_exists(report_path.parent, report_path.name):
            with container:
                ui.label("The report is too large to show.").classes("text-gray-500 italic")
        return
    with container:
        ui.markdown(content).classes("w-full")


def _render_lazy_markdown_report(tabs: ui.tabs, report_tab: ui.tab, report_path: Path) -> None:
    """The markdown report view for jobs whose HTML report is missing.

    The page build only places a loading state. The conversion runs when the
    tab is first shown, so a large report cannot push the build past the
    response deadline. Reopenings reuse the rendered report.
    """
    rendered = {"value": False}
    container = ui.column().classes("w-full")
    with container:
        ui.label("Converting the report...").classes("text-sm text-gray-400 italic")

    async def _activate_report_tab(
        e: ValueChangeEventArguments[str | Tab | TabPanel | None],
    ) -> None:
        if e.value != report_tab.props["name"] or rendered["value"]:
            return
        rendered["value"] = True
        try:
            await _render_report_markdown(container, report_path)
        except BaseException:
            rendered["value"] = False
            raise

    tabs.on_value_change(_activate_report_tab)


def _render_missing_report_state(context: _JobDetailContext) -> None:
    if context.job_info.status == JobStatus.GENERATING_REPORT:
        ui.label("Report is being generated...").classes("text-gray-500 italic")
        return
    if context.job_info.status is JobStatus.COMPLETED:
        ui.label("Report generation failed").classes("text-red-500")
        return
    ui.label("Report will be available when job completes").classes("text-gray-500 italic")


def _render_report_tab(context: _JobDetailContext, tabs: ui.tabs, report_tab: ui.tab) -> None:
    report_path = context.job_dir / "final_report.md"
    pdf_path = context.job_dir / "final_report.pdf"

    # Only show the report when the job completed. The agent may write
    # final_report.md mid-run, but it is not ready for display until the
    # orchestrator marks the job completed.
    if context.job_info.status is not JobStatus.COMPLETED:
        _render_missing_report_state(context)
        return

    if job_file_exists(context.job_dir, report_path.name):
        _render_report_actions(context, report_path, pdf_path)
        # Prefer HTML report (with embedded figures) over raw markdown, when
        # the report route can serve it.
        if _report_html_available(context.job_dir):
            _render_report_html_iframe(context.job_dir, tabs, report_tab)
        else:
            _render_lazy_markdown_report(tabs, report_tab, report_path)
        return

    _render_missing_report_state(context)

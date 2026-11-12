"""WeasyPrint PDF generation with isolated public rendering."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast

from openscientist.artifact_packager import MAX_ARTIFACT_FILE_SIZE_BYTES
from openscientist.public_report_worker import EXIT_REPORT_MISSING, EXIT_RESOURCE_LIMIT
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)
_render_lock = threading.Lock()
_active_public_renders = 0


class PublicReportBusyError(Exception):
    """All public rendering slots are occupied."""


class PublicReportLimitError(Exception):
    """The public report exceeded the renderer's resource limits."""


@contextmanager
def _public_render_slot(limit: int) -> Iterator[None]:
    global _active_public_renders
    with _render_lock:
        if _active_public_renders >= limit:
            raise PublicReportBusyError
        _active_public_renders += 1
    try:
        yield
    finally:
        with _render_lock:
            _active_public_renders -= 1


async def render_public_report_pdf_isolated(
    job_dir: Path,
    excluded_top_level_dirs: tuple[str, ...],
    run_environment: str | None = None,
) -> bytes:
    """Render a public report in a resource-limited child without caching it."""
    limits = get_settings().public_report
    with (
        _public_render_slot(limits.max_concurrent),
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryFile() as errors,
    ):
        async with asyncio.timeout(limits.timeout_seconds):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "openscientist.public_report_worker",
                str(limits.memory_mb),
                str(limits.cpu_seconds),
                str(MAX_ARTIFACT_FILE_SIZE_BYTES),
                json.dumps(
                    {
                        "job_dir": str(job_dir),
                        "excluded_top_level_dirs": excluded_top_level_dirs,
                        "run_environment": run_environment,
                    }
                ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                start_new_session=True,
            )
            try:
                returncode = await process.wait()
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(process.wait(), timeout=5)
        if returncode == EXIT_REPORT_MISSING:
            raise FileNotFoundError
        if returncode in (EXIT_RESOURCE_LIMIT, -signal.SIGKILL, -signal.SIGXCPU, -signal.SIGXFSZ):
            raise PublicReportLimitError
        if returncode != 0:
            errors.seek(max(0, errors.tell() - 16 * 1024))
            logger.error(
                "Public PDF renderer failed for %s with status %s\n%s",
                job_dir.name,
                returncode,
                errors.read(16 * 1024).decode("utf-8", errors="replace"),
            )
            raise RuntimeError("Public PDF rendering failed")
        if output.tell() > MAX_ARTIFACT_FILE_SIZE_BYTES:
            raise PublicReportLimitError
        output.seek(0)
        return output.read(MAX_ARTIFACT_FILE_SIZE_BYTES)


async def render_report_pdf(html_path: Path, pdf_path: Path, job_dir: Path) -> Path:
    """Convert HTML report to PDF via WeasyPrint.

    Runs the CPU-bound WeasyPrint rendering in a thread pool executor.

    Args:
        html_path: Path to the HTML report file.
        pdf_path: Destination for the generated PDF.
        job_dir: Job directory used as base_url for resolving images.

    Returns:
        Path to the generated PDF file.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _render_pdf_sync, html_path, pdf_path, job_dir)


def _render_pdf_sync(html_path: Path, pdf_path: Path, job_dir: Path) -> Path:
    """Synchronous WeasyPrint rendering."""
    from weasyprint import HTML  # type: ignore[import-untyped]

    logger.info("Generating PDF from %s", html_path)
    HTML(filename=str(html_path), base_url=str(job_dir)).write_pdf(str(pdf_path))
    logger.info("PDF generated: %s (%d bytes)", pdf_path, pdf_path.stat().st_size)
    return pdf_path


def render_public_report_pdf(
    job_dir: Path,
    excluded_top_level_dirs: tuple[str, ...],
    run_environment: str | None = None,
) -> bytes:
    """Render a public PDF without reading persisted reports or fetching external resources."""
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher  # type: ignore[import-untyped]

    from openscientist.report.renderer import render_report_html

    html = render_report_html(
        job_dir / "final_report.md",
        job_dir,
        embed_images=True,
        run_environment=run_environment,
        excluded_top_level_dirs=excluded_top_level_dirs,
    )
    return cast(
        bytes, HTML(string=html, url_fetcher=URLFetcher(allowed_protocols={"data"})).write_pdf()
    )

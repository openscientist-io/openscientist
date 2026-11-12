"""WeasyPrint PDF generation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


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

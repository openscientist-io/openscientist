"""WeasyPrint PDF generation.

Converts HTML report to PDF with embedded images.
Runs in a thread pool executor to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

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

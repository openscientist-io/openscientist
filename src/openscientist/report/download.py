"""Report downloads with viewer-specific input exclusions."""

import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, status
from fastapi.responses import Response

from openscientist.artifact_packager import MAX_ARTIFACT_FILE_SIZE_BYTES
from openscientist.job_files import (
    install_file,
    job_file_exists,
    read_job_file_text,
    serve_job_file,
    try_open_job_file,
)
from openscientist.report.pdf import (
    PublicReportBusyError,
    PublicReportLimitError,
    render_public_report_pdf_isolated,
)

logger = logging.getLogger(__name__)


async def report_download_response(
    job_dir: Path,
    download_label: str,
    *,
    public_reader: bool = False,
    run_environment: str | None = None,
) -> Response:
    """Serve a saved report to direct viewers or render an uncached public PDF."""
    if public_reader:
        source = try_open_job_file(
            job_dir, "final_report.md", max_size=MAX_ARTIFACT_FILE_SIZE_BYTES
        )
        if source is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
        source.close()
        try:
            rendered = await render_public_report_pdf_isolated(job_dir, run_environment)
        except PublicReportBusyError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Public report rendering is busy. Try again later.",
                headers={"Retry-After": "5"},
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="The public report took too long to render.",
            ) from exc
        except PublicReportLimitError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The public report exceeds the rendering limits.",
            ) from exc
        except FileNotFoundError as exc:
            logger.warning(
                "Public report rendering failed for %s because the report source is missing or unreadable",
                job_dir.name,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Report not found"
            ) from exc
        except Exception as exc:
            logger.warning("Public report rendering failed for %s: %s", job_dir.name, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="The public report could not be rendered.",
            ) from exc
        filename = quote(f"{download_label}_final_report.pdf")
        return Response(
            rendered,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename}"},
        )

    pdf_file = try_open_job_file(job_dir, "final_report.pdf", max_size=MAX_ARTIFACT_FILE_SIZE_BYTES)
    if pdf_file is not None:
        return serve_job_file(
            pdf_file,
            media_type="application/pdf",
            filename=f"{download_label}_final_report.pdf",
        )
    if job_file_exists(job_dir, "final_report.pdf"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The report is too large to download",
        )

    raw_md = read_job_file_text(job_dir, "final_report.md", max_size=MAX_ARTIFACT_FILE_SIZE_BYTES)
    if raw_md is None and job_file_exists(job_dir, "final_report.md"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The report is too large to download",
        )
    if raw_md is not None:
        try:
            from openscientist.pdf_generator import markdown_to_pdf
            from openscientist.report.processor import strip_figure_tags

            with tempfile.TemporaryDirectory(prefix=f"openscientist_{job_dir.name}_") as tmp_dir:
                stripped_path = Path(tmp_dir) / "_final_report_stripped.md"
                stripped_path.write_text(strip_figure_tags(raw_md), encoding="utf-8")
                generated_pdf = Path(tmp_dir) / "final_report.pdf"
                await asyncio.to_thread(
                    markdown_to_pdf, stripped_path, generated_pdf, run_environment=run_environment
                )
                install_file(job_dir, "final_report.pdf", generated_pdf)

            pdf_file = try_open_job_file(
                job_dir, "final_report.pdf", max_size=MAX_ARTIFACT_FILE_SIZE_BYTES
            )
            if pdf_file is not None:
                return serve_job_file(
                    pdf_file,
                    media_type="application/pdf",
                    filename=f"{download_label}_final_report.pdf",
                )
        except Exception as exc:
            logger.warning("Failed to generate PDF for %s: %s", job_dir.name, exc, exc_info=True)

    for name, media_type in (
        ("final_report.html", "text/html"),
        ("final_report.md", "text/markdown"),
        ("report.pdf", "application/pdf"),
        ("report.md", "text/markdown"),
    ):
        job_file = try_open_job_file(job_dir, name, max_size=MAX_ARTIFACT_FILE_SIZE_BYTES)
        if job_file is not None:
            return serve_job_file(
                job_file,
                media_type=media_type,
                filename=f"{download_label}_{name}",
            )

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

"""Session-authenticated HTTP routes that serve a job's files to readers of the job."""

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from openscientist.api.utils import parse_uuid
from openscientist.artifact_packager import create_artifacts_zip_file
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.webapp_components.share_routes import get_current_user_from_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web/jobs", include_in_schema=False)
CURRENT_SESSION_USER_DEP = Depends(get_current_user_from_session)
SESSION_DEP = Depends(get_session)

# Opens external links in a new tab and leaves #fragment links (the TOC) alone.
_REPORT_LINK_SCRIPT = """<script>
document.addEventListener('click', function(e) {
  var a = e.target.closest('a');
  if (!a) return;
  var href = a.getAttribute('href') || '';
  if (href.startsWith('#')) return;
  e.preventDefault();
  window.open(href, '_blank', 'noopener,noreferrer');
});
</script></body>"""


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def _readable_job_dir(job_id: str, user: User, session: AsyncSession) -> tuple[Job, Path]:
    """Return a job the user may read under RLS, and its directory."""
    await set_current_user(session, user.id)
    job_uuid = parse_uuid(job_id, "job_id")
    result = await session.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None:
        raise _not_found("Job not found or access denied")

    from openscientist.web_app import get_job_manager

    job_dir = get_job_manager().jobs_dir / str(job.id)
    if not job_dir.exists():
        raise _not_found("Job directory not found")
    return job, job_dir


def plot_url(job_id: str, filename: str) -> str:
    """Return the URL that serves one of a job's plot images."""
    return f"{router.prefix}/{job_id}/plots/{filename}"


def report_html_url(job_id: str) -> str:
    """Return the URL that serves a job's rendered HTML report."""
    return f"{router.prefix}/{job_id}/report.html"


@router.get("/{job_id}/artifacts.zip")
async def download_job_artifacts(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    job, job_dir = await _readable_job_dir(job_id, user, session)

    with tempfile.NamedTemporaryFile(
        suffix="_artifacts.zip",
        prefix=f"openscientist_{job.id}_",
        delete=False,
    ) as tmp_file:
        archive_path = Path(tmp_file.name)

    try:
        await run_in_threadpool(
            create_artifacts_zip_file,
            job_dir=job_dir,
            archive_path=archive_path,
            job_id=str(job.id),
        )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    background_tasks.add_task(archive_path.unlink, missing_ok=True)
    return FileResponse(
        path=archive_path,
        media_type="application/zip",
        filename=f"{job_id}_artifacts.zip",
    )


@router.get("/{job_id}/plots/{filename}")
async def get_job_plot(
    job_id: str,
    filename: str,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    """Serve a PNG plot that sits directly in the job's ``provenance/`` directory."""
    _job, job_dir = await _readable_job_dir(job_id, user, session)
    provenance_dir = job_dir / "provenance"
    plot_path = provenance_dir / filename
    if (
        Path(filename).name != filename
        or plot_path.suffix != ".png"
        or plot_path.is_symlink()
        or not plot_path.is_file()
        or plot_path.resolve().parent != provenance_dir.resolve()
    ):
        raise _not_found("Plot not found")
    return FileResponse(path=plot_path, media_type="image/png")


def _render_report_page(job_dir: Path) -> str:
    from openscientist.report.renderer import render_report_html

    try:
        html_content = render_report_html(job_dir / "final_report.md", job_dir, embed_images=True)
    except Exception:
        logger.warning("Failed to re-render HTML with base64 images, using on-disk version")
        html_content = (job_dir / "final_report.html").read_text(encoding="utf-8")
    return html_content.replace("</body>", _REPORT_LINK_SCRIPT, 1)


@router.get("/{job_id}/report.html")
async def get_job_report_html(
    job_id: str,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> HTMLResponse:
    """Serve the job's report as HTML with its figures embedded."""
    _job, job_dir = await _readable_job_dir(job_id, user, session)
    if not (job_dir / "final_report.html").exists():
        raise _not_found("Report not found")
    return HTMLResponse(await run_in_threadpool(_render_report_page, job_dir))

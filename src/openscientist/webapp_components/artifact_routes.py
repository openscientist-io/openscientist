"""HTTP routes that serve a job's artifacts, report and artifacts ZIP to users
who may see the job, off the realtime channel."""

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from openscientist.api.utils import parse_uuid
from openscientist.artifact_packager import artifact_file, create_artifacts_zip_file
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.run_environment import run_environment_text
from openscientist.webapp_components.share_routes import get_current_user_from_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web/jobs", include_in_schema=False)
CURRENT_SESSION_USER_DEP = Depends(get_current_user_from_session)
SESSION_DEP = Depends(get_session)

# The agent writes every artifact and the report, raw HTML included, so the
# browser renders them in an opaque origin, where the session cookie never
# reaches the app. The report keeps its script, which opens links in new tabs.
_SANDBOX = "sandbox"
_REPORT_SANDBOX = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"

_OPEN_LINKS_IN_NEW_TABS = """<script>
document.addEventListener('click', function(e) {
  var a = e.target.closest('a');
  if (!a) return;
  var href = a.getAttribute('href') || '';
  if (href.startsWith('#')) return;
  e.preventDefault();
  window.open(href, '_blank', 'noopener,noreferrer');
});
</script></body>"""


def _sandboxed(policy: str) -> dict[str, str]:
    return {"Content-Security-Policy": policy, "X-Content-Type-Options": "nosniff"}


async def _visible_job(job_id: str, user: User, session: AsyncSession) -> tuple[Job, Path]:
    """The job ``job_id`` and its directory when ``user`` may see it, else 404."""
    await set_current_user(session, user.id)
    job_uuid = parse_uuid(job_id, "job_id")
    job = (await session.execute(select(Job).where(Job.id == job_uuid))).scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    from openscientist.web_app import get_job_manager

    job_dir: Path = get_job_manager().jobs_dir / str(job.id)
    if not job_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job directory not found",
        )
    return job, job_dir


@router.get("/{job_id}/artifacts.zip")
async def download_job_artifacts(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    job, job_dir = await _visible_job(job_id, user, session)

    with tempfile.NamedTemporaryFile(
        suffix="_artifacts.zip",
        prefix=f"openscientist_{job_dir.name}_",
        delete=False,
    ) as tmp_file:
        archive_path = Path(tmp_file.name)

    try:
        await run_in_threadpool(
            create_artifacts_zip_file,
            job_dir=job_dir,
            archive_path=archive_path,
            job_id=job_dir.name,
            version_info=job.version_info,
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


@router.get("/{job_id}/files/{path:path}")
async def job_artifact(
    job_id: str,
    path: str,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    """One of the job's artifacts, as the artifacts ZIP would include it."""
    _, job_dir = await _visible_job(job_id, user, session)
    found = await run_in_threadpool(artifact_file, job_dir, path)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(found, headers=_sandboxed(_SANDBOX))


def _report_html(job_dir: Path, version_info: dict[str, str] | None) -> str | None:
    """The job's report for the browser, with its figures embedded, or None."""
    html_path = job_dir / "final_report.html"
    if not html_path.is_file():
        return None
    try:
        from openscientist.report.renderer import render_report_html

        html = render_report_html(
            job_dir / "final_report.md",
            job_dir,
            embed_images=True,
            run_environment=run_environment_text(version_info),
        )
    except Exception:
        logger.warning("Failed to re-render HTML with base64 images, using on-disk version")
        html = html_path.read_text(encoding="utf-8")
    return html.replace("</body>", _OPEN_LINKS_IN_NEW_TABS, 1)


@router.get("/{job_id}/report.html")
async def job_report(
    job_id: str,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> HTMLResponse:
    """The job's report, rendered from its current files."""
    job, job_dir = await _visible_job(job_id, user, session)
    html = await run_in_threadpool(_report_html, job_dir, job.version_info)
    if html is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return HTMLResponse(html, headers=_sandboxed(_REPORT_SANDBOX))

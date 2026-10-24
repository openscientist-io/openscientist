"""HTTP route that streams a job's artifacts ZIP off the realtime channel."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from openscientist.api.utils import parse_uuid
from openscientist.artifact_packager import create_artifacts_zip_file
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.share_service import withheld_artifact_dirs
from openscientist.webapp_components.share_routes import get_current_user_from_session

router = APIRouter(prefix="/web/jobs", include_in_schema=False)
CURRENT_SESSION_USER_DEP = Depends(get_current_user_from_session)
SESSION_DEP = Depends(get_session)


@router.get("/{job_id}/artifacts.zip")
async def download_job_artifacts(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    await set_current_user(session, user.id)
    job_uuid = parse_uuid(job_id, "job_id")
    result = await session.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    from openscientist.web_app import get_job_manager

    job_dir = get_job_manager().jobs_dir / str(job.id)
    if not job_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job directory not found",
        )

    excluded_dirs = await withheld_artifact_dirs(session, job, user.id)

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
            excluded_top_level_dirs=excluded_dirs,
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

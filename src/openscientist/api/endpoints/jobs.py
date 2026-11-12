"""Job management endpoints."""

import asyncio
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from openscientist.admin_settings import current_admin_settings
from openscientist.api.auth import get_current_user_from_api_key
from openscientist.api.utils import parse_uuid
from openscientist.artifact_packager import create_artifacts_zip_file
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.file_loader import FileTooBigError, validate_uploaded_file
from openscientist.job.types import JobStatus
from openscientist.job_manager import JobManager
from openscientist.report.download import report_download_response
from openscientist.share_service import set_job_public, withheld_artifact_dirs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


# One Starlette spool size in flight at a time, so no part of an upload is
# held in memory whole, matching the parser's 1 MiB spool threshold.
UPLOAD_CHUNK_SIZE = 1024 * 1024
# The per-request file count cap. The total bytes per request is the
# max_file_size_mb value from the admin settings row.
MAX_UPLOAD_FILES_PER_REQUEST = 100

CURRENT_USER_DEP = Depends(get_current_user_from_api_key)
SESSION_DEP = Depends(get_session)


class JobCreate(BaseModel):
    """Request body for creating a new job."""

    short_title: str | None = Field(
        None,
        max_length=100,
        description="Optional short display label for the job (truncated to 100 chars).",
        examples=["Kinase inhibitor analysis"],
    )
    description: str | None = Field(
        None,
        description="Detailed job description",
        examples=["Comparing treated vs control samples to explain pathway shifts"],
    )
    research_question: str = Field(
        ...,
        min_length=1,
        description="Research question for the analysis",
        examples=["What are the key binding sites in this protein structure?"],
    )
    max_iterations: int = Field(
        5,
        ge=1,
        le=20,
        description="Maximum number of analysis iterations",
    )
    use_hypotheses: bool = Field(
        False,
        description="Whether to enable hypothesis tracking and testing tools for this job",
    )
    investigation_mode: str = Field(
        "autonomous",
        description="Investigation mode: 'autonomous' or 'coinvestigate'",
        pattern="^(autonomous|coinvestigate)$",
    )
    pdb_code: str | None = Field(
        None,
        max_length=10,
        description="PDB code if analyzing existing structure",
        examples=["1ABC"],
    )
    space_group: str | None = Field(
        None,
        max_length=50,
        description="Crystal space group",
        examples=["P212121"],
    )


class JobResponse(BaseModel):
    """Response for a job."""

    id: str = Field(..., description="Job ID")
    research_question: str = Field(
        ..., description="Research question that drives the agent prompt"
    )
    short_title: str | None = Field(None, description="Optional short display label")
    description: str | None = Field(None, description="Job description")
    status: str = Field(..., description="Job status")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC)")
    max_iterations: int = Field(..., description="Maximum iterations")
    current_iteration: int = Field(..., description="Current iteration number")
    pdb_code: str | None = Field(None, description="PDB code")
    space_group: str | None = Field(None, description="Space group")
    queue_reason: str | None = Field(
        None, description="Why a queued job waits on a budget limit, if it is held"
    )
    stop_note: str | None = Field(
        None, description="Why a completed job stopped early at a budget limit, if it did"
    )
    is_public: bool = Field(False, description="Whether any signed-in user can read this job")


class JobListResponse(BaseModel):
    """Response for listing jobs."""

    jobs: list[JobResponse] = Field(..., description="List of jobs")
    total: int = Field(..., description="Total number of jobs")


class JobStatusResponse(BaseModel):
    """Response for job status check."""

    id: str = Field(..., description="Job ID")
    status: str = Field(..., description="Current job status")
    current_iteration: int = Field(..., description="Current iteration")
    max_iterations: int = Field(..., description="Maximum iterations")
    error_message: str | None = Field(None, description="Error message if failed")
    queue_reason: str | None = Field(
        None, description="Why a queued job waits on a budget limit, if it is held"
    )
    stop_note: str | None = Field(
        None, description="Why a completed job stopped early at a budget limit, if it did"
    )


class JobDetailResponse(JobResponse):
    """Detailed response for a single job."""

    investigation_mode: str | None = Field(None, description="Investigation mode")
    result_summary: str | None = Field(None, description="Final result summary")
    error_message: str | None = Field(None, description="Error message if failed")
    version_info: dict[str, str] | None = Field(
        None,
        description=(
            "Environment the run recorded: OpenScientist commit and build time, agent "
            "harness and its version, container id. Stamped once per run."
        ),
    )


class _JobResponseFields(TypedDict):
    id: str
    research_question: str
    short_title: str | None
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    max_iterations: int
    current_iteration: int
    pdb_code: str | None
    space_group: str | None
    queue_reason: str | None
    stop_note: str | None
    is_public: bool


class JobVisibilityUpdate(BaseModel):
    """Request to change who can read a job."""

    is_public: bool = Field(
        ...,
        description=(
            "True lets any signed-in user read the job, without its uploaded "
            "input files; false restores owner/share-only access"
        ),
    )


async def get_job_by_id(
    job_id: str,
    user: User,
    session: AsyncSession,
) -> Job | None:
    """Get a job by ID. RLS policies restrict the result to the user's own or shared jobs."""
    await set_current_user(session, user.id)

    job_uuid = parse_uuid(job_id, "job_id")

    stmt = select(Job).where(Job.id == job_uuid)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _get_job_manager() -> JobManager:
    """Get the job manager instance."""
    # Import lazily to avoid circular import with web_app
    from openscientist.web_app import get_job_manager

    return get_job_manager()


def _get_jobs_dir() -> Path:
    """Resolve the filesystem jobs directory from the active job manager."""
    manager = _get_job_manager()
    jobs_dir = getattr(manager, "jobs_dir", Path("jobs"))
    return Path(jobs_dir)


def _job_response_fields(job: Job) -> _JobResponseFields:
    """Return common response fields shared by job response models."""
    return {
        "id": str(job.id),
        "research_question": job.research_question,
        "short_title": job.short_title,
        "description": job.description,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "max_iterations": job.max_iterations,
        "current_iteration": job.current_iteration,
        "pdb_code": job.pdb_code,
        "space_group": job.space_group,
        "queue_reason": job.queue_reason,
        "stop_note": job.stop_note,
        "is_public": job.is_public,
    }


def _job_to_response(job: Job) -> JobResponse:
    """Convert Job model to JobResponse."""
    return JobResponse(**_job_response_fields(job))


def _job_to_detail_response(job: Job) -> JobDetailResponse:
    """Convert Job model to JobDetailResponse."""
    return JobDetailResponse(
        **_job_response_fields(job),
        investigation_mode=job.investigation_mode,
        result_summary=job.result_summary,
        error_message=job.error_message,
        version_info=job.version_info,
    )


def _with_body_error_loc(errors: list[Any]) -> list[dict[str, Any]]:
    """Prefix Pydantic errors with 'body' for FastAPI validation responses."""
    normalized: list[dict[str, Any]] = []
    for error in errors:
        raw_loc = error.get("loc", ())
        if isinstance(raw_loc, tuple):
            loc = raw_loc
        elif isinstance(raw_loc, list):
            loc = tuple(raw_loc)
        elif raw_loc is None:
            loc = ()
        else:
            loc = (raw_loc,)

        if not loc or loc[0] != "body":
            loc = ("body", *loc)

        normalized.append({**error, "loc": loc})
    return normalized


def _validate_job_payload(payload: dict[str, Any]) -> JobCreate:
    """Validate request payload and convert to JobCreate."""
    try:
        return JobCreate.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(_with_body_error_loc(exc.errors())) from exc


def _extract_upload_files(form: FormData) -> list[StarletteUploadFile]:
    """Extract uploaded files from multipart form fields."""
    upload_files: list[StarletteUploadFile] = []
    for field_name in ("data_files", "data_files[]"):
        for value in form.getlist(field_name):
            if isinstance(value, StarletteUploadFile) and value.filename:
                upload_files.append(value)
    return upload_files


def _extract_job_payload_from_form(form: FormData) -> dict[str, Any]:
    """Build a JobCreate payload from multipart form fields."""
    payload: dict[str, Any] = {}
    for field_name in JobCreate.model_fields:
        value = form.get(field_name)
        if value is None or isinstance(value, StarletteUploadFile):
            continue
        payload[field_name] = value

    description = payload.get("description")
    if isinstance(description, str) and not description.strip():
        payload["description"] = None

    return payload


def _build_unique_upload_path(temp_dir: Path, filename: str) -> Path:
    """Create a safe, unique destination path for an uploaded file."""
    safe_name = Path(filename).name
    if not safe_name:
        raise ValueError("Uploaded file is missing a filename")

    candidate = temp_dir / safe_name
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    index = 1
    while candidate.exists():
        candidate = temp_dir / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


async def _stream_upload_to_file(
    upload: StarletteUploadFile,
    target_path: Path,
    limit_bytes: int,
    max_file_size_bytes: int,
) -> int:
    """Copy one upload to ``target_path`` in chunks and return the bytes written.

    Past ``limit_bytes`` the partial file is deleted and FileTooBigError is raised.
    """
    written = 0
    try:
        with open(target_path, "wb") as out:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                written += len(chunk)
                if written > limit_bytes:
                    if limit_bytes >= max_file_size_bytes:
                        raise FileTooBigError(
                            f"File is {written / 1024 / 1024:.1f}MB, "
                            f"exceeds the limit of {max_file_size_bytes / 1024 / 1024:.0f}MB"
                        )
                    raise FileTooBigError(
                        f"Uploaded files exceed the total limit of "
                        f"{max_file_size_bytes / 1024 / 1024:.0f}MB"
                    )
                await run_in_threadpool(out.write, chunk)
    except FileTooBigError:
        target_path.unlink(missing_ok=True)
        raise
    return written


async def _persist_uploaded_files(
    upload_files: list[StarletteUploadFile],
    temp_dir: Path,
    max_file_size_bytes: int,
) -> list[Path]:
    """Write uploads to temporary disk paths, one chunk in flight at a time.

    Raises FileTooBigError when a part or the request total exceeds the limit.
    """
    persisted_files: list[Path] = []
    total_bytes = 0
    for upload in upload_files:
        filename = upload.filename or ""
        try:
            target_path = _build_unique_upload_path(temp_dir, filename)
            remaining = max_file_size_bytes - total_bytes
            if remaining <= 0:
                raise FileTooBigError(
                    f"Uploaded files exceed the total limit of "
                    f"{max_file_size_bytes / 1024 / 1024:.0f}MB"
                )
            written = await _stream_upload_to_file(
                upload, target_path, remaining, max_file_size_bytes
            )
            total_bytes += written
            # The size limit comes from the database, which must stay off the loop.
            await asyncio.to_thread(validate_uploaded_file, target_path)
            persisted_files.append(target_path)
        finally:
            await upload.close()

    return persisted_files


async def _parse_job_create_request(
    request: Request,
) -> tuple[JobCreate, list[StarletteUploadFile]]:
    """Parse job creation payload from JSON or multipart form-data."""
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        # One extra part past the cap, so a request with MAX+1 files reaches the
        # endpoint's 413 instead of the parser's own 400.
        form = await request.form(max_files=MAX_UPLOAD_FILES_PER_REQUEST + 1)
        payload = _extract_job_payload_from_form(form)
        return _validate_job_payload(payload), _extract_upload_files(form)

    try:
        payload = await request.json()
    except ValueError as exc:
        raise RequestValidationError(
            [
                {
                    "type": "value_error.jsondecode",
                    "loc": ("body",),
                    "msg": "Invalid JSON payload",
                    "input": None,
                }
            ]
        ) from exc

    if not isinstance(payload, dict):
        raise RequestValidationError(
            [
                {
                    "type": "type_error.dict",
                    "loc": ("body",),
                    "msg": "Input should be an object",
                    "input": payload,
                }
            ]
        )

    return _validate_job_payload(payload), []


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> JobResponse:
    """Create a new scientific analysis job from a JSON or multipart ``data_files`` payload."""
    # Same approval rule the jobs INSERT policy enforces.
    if not await session.scalar(text("SELECT user_can_start_jobs(:id)"), {"id": user.id}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your account is pending administrator approval. You cannot start new jobs yet."
            ),
        )

    job_data, upload_files = await _parse_job_create_request(request)

    if len(upload_files) > MAX_UPLOAD_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Too many files. Maximum number of files is {MAX_UPLOAD_FILES_PER_REQUEST}.",
        )

    job_uuid = uuid4()
    job_manager = _get_job_manager()
    try:
        with tempfile.TemporaryDirectory(prefix="openscientist_api_upload_") as upload_tmp:
            max_file_size_bytes = (
                (await current_admin_settings()).max_file_size_mb * 1024 * 1024
                if upload_files
                else 0
            )
            data_files = await _persist_uploaded_files(
                upload_files, Path(upload_tmp), max_file_size_bytes
            )
            await job_manager.create_job(
                job_id=str(job_uuid),
                research_question=job_data.research_question,
                data_files=data_files,
                max_iterations=job_data.max_iterations,
                use_hypotheses=job_data.use_hypotheses,
                auto_start=True,
                investigation_mode=job_data.investigation_mode,
                owner_id=str(user.id),
                short_title=job_data.short_title,
                description=job_data.description,
                pdb_code=job_data.pdb_code,
                space_group=job_data.space_group,
            )
        logger.info("Created job %s for user %s", job_uuid, user.email)
    except FileTooBigError as e:
        logger.info("Rejected oversized file upload for user %s: %s", user.email, e)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(e),
        ) from e
    except ValueError as e:
        logger.info("Rejected job creation for user %s: %s", user.email, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.exception("Failed to create job for user %s", user.email)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create job",
        ) from e

    # Re-read via this session so the response reflects RLS-visible persisted state.
    created_job = await get_job_by_id(str(job_uuid), user, session)
    if created_job is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Job created but could not be loaded",
        )

    return _job_to_response(created_job)


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status_filter: str | None = Query(
        None,
        alias="status",
        description="Filter by status",
        examples=["running", "completed", "failed"],
    ),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of jobs to return",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Number of jobs to skip",
    ),
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> JobListResponse:
    """List jobs owned by the authenticated user. Shared jobs appear under the /shares endpoints."""
    await set_current_user(session, user.id)

    # Explicit owner filter so shared jobs don't leak in.
    stmt = select(Job).where(Job.owner_id == user.id).order_by(Job.created_at.desc())

    if status_filter:
        stmt = stmt.where(Job.status == status_filter)

    count_stmt = select(func.count(Job.id)).where(Job.owner_id == user.id)
    if status_filter:
        count_stmt = count_stmt.where(Job.status == status_filter)
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    stmt = stmt.limit(limit).offset(offset)

    result = await session.execute(stmt)
    jobs = result.scalars().all()

    return JobListResponse(
        jobs=[_job_to_response(job) for job in jobs],
        total=total,
    )


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> JobDetailResponse:
    """Get detailed information about a specific job."""
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    return _job_to_detail_response(job)


@router.get("/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> JobStatusResponse:
    """Get the current status of a job."""
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    return JobStatusResponse(
        id=str(job.id),
        status=job.status,
        current_iteration=job.current_iteration,
        max_iterations=job.max_iterations,
        error_message=job.error_message,
        queue_reason=job.queue_reason,
        stop_note=job.stop_note,
    )


@router.post("/{job_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    job_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> None:
    """Cancel an active job. It stops at the next iteration checkpoint."""
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    if job.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the job owner can cancel a job",
        )

    if JobStatus(job.status) not in JobManager.ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel job with status '{job.status}'",
        )

    job_manager = _get_job_manager()
    try:
        await job_manager.cancel_job(str(job.id))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    logger.info("Cancelled job %s for user %s", job.id, user.email)


@router.put("/{job_id}/visibility", response_model=JobDetailResponse)
async def update_job_visibility(
    job_id: str,
    update: JobVisibilityUpdate,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> JobDetailResponse:
    """Make a job readable by any signed-in user, or private again (owner only).

    A public job is reachable by ID or link but stays off other users' job
    pages. A reader who is neither the owner nor a share recipient does not
    receive the job's uploaded input files.
    """
    await set_current_user(session, user.id)
    job = await set_job_public(
        session,
        user.id,
        job_id=job_id,
        is_public=update.is_public,
        not_owned_detail="Only the job owner can change its visibility",
    )
    logger.info("Set job %s is_public=%s for user %s", job.id, job.is_public, user.email)
    return _job_to_detail_response(job)


@router.get("/{job_id}/report")
async def download_report(
    job_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> Response:
    """Download the final analysis report."""
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    if job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Report only available for completed jobs",
        )

    job_dir = _get_jobs_dir() / str(job.id)
    download_label = (job.short_title or job.research_question[:50]).replace(" ", "_")

    excluded_dirs = await withheld_artifact_dirs(session, job, user.id)
    return await report_download_response(
        job_dir, download_label, excluded_top_level_dirs=excluded_dirs
    )


@router.get("/{job_id}/artifacts")
async def download_artifacts(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    """Download all job artifacts as a ZIP archive."""
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    job_dir = _get_jobs_dir() / str(job.id)

    if not job_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job directory not found",
        )

    # A job readable only because it is public comes without the owner's uploads.
    excluded_dirs = await withheld_artifact_dirs(session, job, user.id)

    # Build ZIP archive on disk to avoid holding large archives in memory.
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
            version_info=job.version_info,
            excluded_top_level_dirs=excluded_dirs,
        )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    background_tasks.add_task(archive_path.unlink, missing_ok=True)

    download_label = (job.short_title or job.research_question[:50]).replace(" ", "_")
    return FileResponse(
        path=archive_path,
        media_type="application/zip",
        filename=f"{download_label}_artifacts.zip",
    )

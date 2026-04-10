"""
Job management endpoints.

Provides REST API endpoints for creating, listing, monitoring, and managing
OpenScientist scientific analysis jobs.
"""

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
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from openscientist.api.auth import get_current_user_from_api_key
from openscientist.api.utils import parse_uuid
from openscientist.artifact_packager import create_artifacts_zip_file
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.file_loader import FileTooBigError, validate_uploaded_file
from openscientist.job_manager import JobManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


CURRENT_USER_DEP = Depends(get_current_user_from_api_key)
SESSION_DEP = Depends(get_session)


# Pydantic models for request/response
class JobCreate(BaseModel):
    """Request body for creating a new job."""

    title: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Job title",
        examples=["Analyze protein structure X"],
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
    title: str = Field(..., description="Job title")
    description: str | None = Field(None, description="Job description")
    status: str = Field(..., description="Job status")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC)")
    max_iterations: int = Field(..., description="Maximum iterations")
    current_iteration: int = Field(..., description="Current iteration number")
    pdb_code: str | None = Field(None, description="PDB code")
    space_group: str | None = Field(None, description="Space group")


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


class JobDetailResponse(JobResponse):
    """Detailed response for a single job."""

    research_question: str | None = Field(None, description="Research question")
    investigation_mode: str | None = Field(None, description="Investigation mode")
    result_summary: str | None = Field(None, description="Final result summary")
    error_message: str | None = Field(None, description="Error message if failed")


class _JobResponseFields(TypedDict):
    id: str
    title: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    max_iterations: int
    current_iteration: int
    pdb_code: str | None
    space_group: str | None


async def get_job_by_id(
    job_id: str,
    user: User,
    session: AsyncSession,
) -> Job | None:
    """
    Get a job by ID, verifying the user has access.

    Uses RLS to ensure users can only access their own jobs or shared jobs.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Query job (RLS policies will filter access)
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
        "title": job.title,
        "description": job.description,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "max_iterations": job.max_iterations,
        "current_iteration": job.current_iteration,
        "pdb_code": job.pdb_code,
        "space_group": job.space_group,
    }


def _job_to_response(job: Job) -> JobResponse:
    """Convert Job model to JobResponse."""
    return JobResponse(**_job_response_fields(job))


def _job_to_detail_response(job: Job) -> JobDetailResponse:
    """Convert Job model to JobDetailResponse."""
    return JobDetailResponse(
        **_job_response_fields(job),
        research_question=job.title,
        investigation_mode=job.investigation_mode,
        result_summary=job.result_summary,
        error_message=job.error_message,
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


async def _persist_uploaded_files(
    upload_files: list[StarletteUploadFile],
    temp_dir: Path,
) -> list[Path]:
    """Write uploaded files to temporary disk paths and return those paths."""
    persisted_files: list[Path] = []
    for upload in upload_files:
        try:
            target_path = _build_unique_upload_path(temp_dir, upload.filename or "")
            content = await upload.read()
            validate_uploaded_file(target_path, content)
            target_path.write_bytes(content)
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
        form = await request.form()
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
    """
    Create a new scientific analysis job.

    The job will be queued and started automatically. Use the GET /jobs/{id}/status
    endpoint to monitor progress.

    Accepts either:
    - JSON payload (`application/json`) without file uploads
    - Multipart form-data with optional repeated `data_files` file fields
    """
    if not user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your account is pending administrator approval. You cannot start new jobs yet."
            ),
        )

    job_data, upload_files = await _parse_job_create_request(request)

    # Create job via JobManager (database + filesystem structure).
    job_uuid = uuid4()
    job_manager = _get_job_manager()
    try:
        with tempfile.TemporaryDirectory(prefix="openscientist_api_upload_") as upload_tmp:
            data_files = await _persist_uploaded_files(upload_files, Path(upload_tmp))
            job_manager.create_job(
                job_id=str(job_uuid),
                research_question=job_data.research_question,
                data_files=data_files,
                max_iterations=job_data.max_iterations,
                use_hypotheses=job_data.use_hypotheses,
                auto_start=True,
                investigation_mode=job_data.investigation_mode,
                owner_id=str(user.id),
                title=job_data.title,
                description=job_data.description,
                pdb_code=job_data.pdb_code,
                space_group=job_data.space_group,
            )
        logger.info("Created job %s for user %s", job_uuid, user.email)
    except FileTooBigError as e:
        logger.info("Rejected oversized file upload for user %s: %s", user.email, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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

    # Load job via API session so response reflects RLS-visible persisted state.
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
    """
    List jobs owned by the authenticated user.

    Returns only jobs where the user is the owner.
    Shared jobs are available via the /shares endpoints.
    Uses Row-Level Security as an additional access control layer.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Build query — explicit owner filter so shared jobs don't leak in
    stmt = select(Job).where(Job.owner_id == user.id).order_by(Job.created_at.desc())

    # Apply status filter
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)

    # Get total count (for pagination)
    count_stmt = select(func.count(Job.id)).where(Job.owner_id == user.id)
    if status_filter:
        count_stmt = count_stmt.where(Job.status == status_filter)
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    # Apply pagination
    stmt = stmt.limit(limit).offset(offset)

    # Execute query
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
    """
    Get detailed information about a specific job.

    Returns job metadata, status, and results if available.
    """
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
    """
    Get the current status of a job.

    Lightweight endpoint for polling job progress.
    """
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
    )


@router.post("/{job_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    job_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> None:
    """
    Cancel a pending, running, or queued job.

    The job will stop at the next iteration checkpoint.
    """
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    # Cancel is owner-only
    if job.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the job owner can cancel a job",
        )

    if job.status not in ["pending", "running", "queued"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel job with status '{job.status}'",
        )

    # Cancel via JobManager
    job_manager = _get_job_manager()
    try:
        job_manager.cancel_job(str(job.id))
        logger.info("Cancelled job %s for user %s", job.id, user.email)
    except Exception as e:
        logger.exception("Failed to cancel job %s", job.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel job",
        ) from e


@router.get("/{job_id}/report")
async def download_report(
    job_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    """
    Download the final analysis report (PDF or Markdown).

    Only available for completed jobs.
    """
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

    # Look for report file
    job_dir = _get_jobs_dir() / str(job.id)

    # Regenerate PDF from markdown if markdown exists
    for md_name, pdf_name in [
        ("final_report.md", "final_report.pdf"),
        ("report.md", "report.pdf"),
    ]:
        md_path = job_dir / md_name
        pdf_path = job_dir / pdf_name
        if md_path.exists():
            try:
                from openscientist.pdf_generator import markdown_to_pdf

                markdown_to_pdf(md_path, pdf_path)
            except Exception:
                logger.warning("Failed to regenerate %s", pdf_name, exc_info=True)

    # Try PDF first, then Markdown
    report_files = [
        (job_dir / "final_report.pdf", "application/pdf"),
        (job_dir / "final_report.md", "text/markdown"),
        (job_dir / "report.pdf", "application/pdf"),
        (job_dir / "report.md", "text/markdown"),
    ]

    for report_file, media_type in report_files:
        if report_file.exists():
            return FileResponse(
                path=report_file,
                media_type=media_type,
                filename=f"{job.title.replace(' ', '_')}_{report_file.name}",
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Report not found",
    )


@router.get("/{job_id}/artifacts")
async def download_artifacts(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> FileResponse:
    """
    Download all job artifacts as a ZIP archive.

    Includes plots, data files, and reports.
    """
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

    # Build ZIP archive on disk to avoid holding large archives in memory.
    with tempfile.NamedTemporaryFile(
        suffix="_artifacts.zip",
        prefix=f"openscientist_{job.id}_",
        delete=False,
    ) as tmp_file:
        archive_path = Path(tmp_file.name)

    try:
        create_artifacts_zip_file(job_dir=job_dir, archive_path=archive_path, job_id=str(job.id))
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    background_tasks.add_task(archive_path.unlink, missing_ok=True)

    return FileResponse(
        path=archive_path,
        media_type="application/zip",
        filename=f"{job.title.replace(' ', '_')}_artifacts.zip",
    )

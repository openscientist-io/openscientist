"""
Job management endpoints.

Provides REST API endpoints for creating, listing, monitoring, and managing
SHANDY crystallography analysis jobs.
"""

import io
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional, TypedDict
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import get_current_user_from_api_key
from shandy.database.models import Job, JobShare, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_session
from shandy.job_manager import JobManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


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
    description: Optional[str] = Field(
        None,
        description="Detailed job description",
        examples=["Investigating binding sites in protein X using crystallography data"],
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
    use_skills: bool = Field(
        True,
        description="Whether to use specialized analysis skills (all enabled skills are available)",
    )
    investigation_mode: str = Field(
        "autonomous",
        description="Investigation mode: 'autonomous' or 'coinvestigate'",
        pattern="^(autonomous|coinvestigate)$",
    )
    pdb_code: Optional[str] = Field(
        None,
        max_length=10,
        description="PDB code if analyzing existing structure",
        examples=["1ABC"],
    )
    space_group: Optional[str] = Field(
        None,
        max_length=50,
        description="Crystal space group",
        examples=["P212121"],
    )


class JobResponse(BaseModel):
    """Response for a job."""

    id: str = Field(..., description="Job ID")
    title: str = Field(..., description="Job title")
    description: Optional[str] = Field(None, description="Job description")
    status: str = Field(..., description="Job status")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC)")
    max_iterations: int = Field(..., description="Maximum iterations")
    current_iteration: int = Field(..., description="Current iteration number")
    pdb_code: Optional[str] = Field(None, description="PDB code")
    space_group: Optional[str] = Field(None, description="Space group")


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
    error_message: Optional[str] = Field(None, description="Error message if failed")


class JobDetailResponse(JobResponse):
    """Detailed response for a single job."""

    research_question: Optional[str] = Field(None, description="Research question")
    investigation_mode: Optional[str] = Field(None, description="Investigation mode")
    result_summary: Optional[str] = Field(None, description="Final result summary")
    error_message: Optional[str] = Field(None, description="Error message if failed")


class _JobResponseFields(TypedDict):
    id: str
    title: str
    description: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime
    max_iterations: int
    current_iteration: int
    pdb_code: Optional[str]
    space_group: Optional[str]


async def get_job_by_id(
    job_id: str,
    user: User,
    session: AsyncSession,
) -> Optional[Job]:
    """
    Get a job by ID, verifying the user has access.

    Uses RLS to ensure users can only access their own jobs or shared jobs.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Query job (RLS policies will filter access)
    try:
        job_uuid = UUID(job_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid job ID format",
        ) from e

    stmt = select(Job).where(Job.id == job_uuid)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _get_job_manager() -> JobManager:
    """Get the job manager instance."""
    # Import lazily to avoid circular import with web_app
    from shandy.web_app import get_job_manager

    return get_job_manager()


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
    # Load legacy config.json for research_question if not in DB
    research_question = None
    investigation_mode = None

    try:
        job_dir = Path("jobs") / str(job.id)
        config_file = job_dir / "config.json"
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
                research_question = config.get("research_question")
                investigation_mode = config.get("investigation_mode", "autonomous")
    except Exception as e:
        logger.warning("Failed to load config for job %s: %s", job.id, e)

    return JobDetailResponse(
        **_job_response_fields(job),
        research_question=research_question,
        investigation_mode=investigation_mode,
        result_summary=job.result_summary,
        error_message=job.error_message,
    )


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    job_data: JobCreate,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> JobResponse:
    """
    Create a new crystallography analysis job.

    The job will be queued and started automatically. Use the GET /jobs/{id}/status
    endpoint to monitor progress.

    Note: File uploads are not yet supported via the REST API. Use the web interface
    to upload data files, or provide a PDB code for analysis.
    """
    # Create job via JobManager (database + filesystem structure).
    job_uuid = uuid4()
    job_manager = _get_job_manager()
    try:
        job_manager.create_job(
            job_id=str(job_uuid),
            research_question=job_data.research_question,
            data_files=[],  # TODO: Support file uploads in API
            max_iterations=job_data.max_iterations,
            use_skills=job_data.use_skills,
            auto_start=True,
            investigation_mode=job_data.investigation_mode,
            owner_id=str(user.id),
            title=job_data.title,
            description=job_data.description,
            pdb_code=job_data.pdb_code,
            space_group=job_data.space_group,
        )
        logger.info("Created job %s for user %s", job_uuid, user.email)
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
    status_filter: Optional[str] = Query(
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
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
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
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
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
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
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
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
):
    """
    Cancel a running or queued job.

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

        # Update database
        job.status = "cancelled"
        await session.commit()

        logger.info("Cancelled job %s for user %s", job.id, user.email)
    except Exception as e:
        logger.exception("Failed to cancel job %s", job.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel job",
        ) from e


@router.post("/{job_id}/regenerate-report", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_report(
    job_id: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
):
    """
    Regenerate the final report for a completed or failed job.

    Re-runs only the report generation phase using the existing
    knowledge state. The job status will change to 'generating_report'
    while in progress.
    """
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    # Regenerate requires owner or edit-level share
    if job.owner_id != user.id:
        share_stmt = select(JobShare.permission_level).where(
            JobShare.job_id == job.id,
            JobShare.shared_with_user_id == user.id,
        )
        share_result = await session.execute(share_stmt)
        permission = share_result.scalar_one_or_none()
        if permission != "edit":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You need edit permission to regenerate the report",
            )

    if job.status not in ["completed", "failed"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only regenerate report for completed or failed jobs (current: '{job.status}')",
        )

    job_manager = _get_job_manager()
    try:
        job_manager.regenerate_report(str(job.id))
        logger.info("Started report regeneration for job %s (user %s)", job.id, user.email)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    return {"detail": "Report regeneration started", "job_id": str(job.id)}


@router.get("/{job_id}/report")
async def download_report(
    job_id: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
):
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
    job_dir = Path("jobs") / str(job.id)

    # Regenerate PDF from markdown if markdown exists
    for md_name, pdf_name in [
        ("final_report.md", "final_report.pdf"),
        ("report.md", "report.pdf"),
    ]:
        md_path = job_dir / md_name
        pdf_path = job_dir / pdf_name
        if md_path.exists():
            try:
                from shandy.pdf_generator import markdown_to_pdf

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
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
):
    """
    Download all job artifacts as a ZIP archive.

    Includes plots, data files, reports, and configuration files.
    """
    job = await get_job_by_id(job_id, user, session)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    job_dir = Path("jobs") / str(job.id)

    if not job_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job directory not found",
        )

    # Create ZIP archive in memory
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # Add all files recursively
        for file_path in job_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(job_dir)
                zip_file.write(file_path, arcname)

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={job.title.replace(' ', '_')}_artifacts.zip"
        },
    )

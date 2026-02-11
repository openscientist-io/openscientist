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
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import get_current_user_from_api_key
from shandy.database.models import Job, JobSkill, Skill, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_session
from shandy.job_manager import JobManager
from shandy.skill_relevance import SkillRelevanceService
from shandy.web_app import get_job_manager

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
        description="Whether to use specialized analysis skills",
    )
    skill_ids: Optional[List[str]] = Field(
        None,
        description="Specific skill IDs to use (manual selection)",
    )
    auto_match_skills: bool = Field(
        True,
        description="Automatically match relevant skills based on research question",
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

    jobs: List[JobResponse] = Field(..., description="List of jobs")
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
    stmt = select(Job).where(Job.id == UUID(job_id))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _get_job_manager() -> JobManager:
    """Get the job manager instance."""
    return get_job_manager()


async def _attach_skills_to_job(
    session: AsyncSession,
    job_id: UUID,
    skill_ids: Optional[List[str]],
    auto_match: bool,
    research_question: str,
) -> None:
    """
    Attach skills to a job.

    If skill_ids provided, uses those specific skills.
    If auto_match is True and no skill_ids, auto-matches based on research question.

    Args:
        session: Database session
        job_id: Job ID to attach skills to
        skill_ids: Optional list of specific skill IDs
        auto_match: Whether to auto-match skills
        research_question: Research question for auto-matching
    """
    skills_to_attach: List[tuple[UUID, float | None, str | None]] = []

    if skill_ids:
        # Manual selection - attach specific skills
        for skill_id_str in skill_ids:
            try:
                skill_uuid = UUID(skill_id_str)
                # Verify skill exists and is enabled
                stmt = select(Skill).where(
                    Skill.id == skill_uuid,
                    Skill.is_enabled == True,  # noqa: E712
                )
                result = await session.execute(stmt)
                skill = result.scalar_one_or_none()
                if skill:
                    skills_to_attach.append((skill.id, None, None))
            except ValueError:
                logger.warning("Invalid skill ID: %s", skill_id_str)
                continue

    elif auto_match and research_question:
        # Auto-match skills based on research question
        service = SkillRelevanceService()
        scored_skills = await service.find_relevant_skills(
            session=session,
            prompt=research_question,
            score_threshold=0.3,
            max_results=5,
        )
        for scored in scored_skills:
            skills_to_attach.append((scored.skill_id, scored.score, scored.match_reason))

    # Create JobSkill records
    for skill_id, score, reason in skills_to_attach:
        job_skill = JobSkill(
            job_id=job_id,
            skill_id=skill_id,
            is_enabled=True,
            relevance_score=score,
            match_reason=reason,
        )
        session.add(job_skill)

    if skills_to_attach:
        await session.commit()
        logger.info(
            "Attached %d skills to job %s",
            len(skills_to_attach),
            job_id,
        )


def _job_to_response(job: Job) -> JobResponse:
    """Convert Job model to JobResponse."""
    return JobResponse(
        id=str(job.id),
        title=job.title,
        description=job.description,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        max_iterations=job.max_iterations,
        current_iteration=job.current_iteration,
        pdb_code=job.pdb_code,
        space_group=job.space_group,
    )


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
        id=str(job.id),
        title=job.title,
        description=job.description,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        max_iterations=job.max_iterations,
        current_iteration=job.current_iteration,
        pdb_code=job.pdb_code,
        space_group=job.space_group,
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
    # Set RLS context
    await set_current_user(session, user.id)

    # Create job in database
    job = Job(
        owner_id=user.id,
        title=job_data.title,
        description=job_data.description,
        status="pending",
        max_iterations=job_data.max_iterations,
        current_iteration=0,
        pdb_code=job_data.pdb_code,
        space_group=job_data.space_group,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # Attach skills to the job
    if job_data.use_skills:
        try:
            await _attach_skills_to_job(
                session=session,
                job_id=job.id,
                skill_ids=job_data.skill_ids,
                auto_match=job_data.auto_match_skills,
                research_question=job_data.research_question,
            )
        except Exception as e:
            logger.warning("Failed to attach skills to job %s: %s", job.id, e)
            # Don't fail job creation if skill attachment fails

    # Create job via JobManager (creates filesystem structure)
    job_manager = _get_job_manager()
    try:
        job_manager.create_job(
            job_id=str(job.id),  # Use UUID as job_id
            research_question=job_data.research_question,
            data_files=[],  # TODO: Support file uploads in API
            max_iterations=job_data.max_iterations,
            use_skills=job_data.use_skills,
            auto_start=True,
            investigation_mode=job_data.investigation_mode,
        )
        logger.info("Created job %s for user %s", job.id, user.email)
    except Exception as e:
        # Rollback database if job creation fails
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create job: {str(e)}",
        )

    return _job_to_response(job)


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
    List jobs accessible to the authenticated user.

    Returns jobs owned by the user plus any jobs shared with them.
    Uses Row-Level Security to enforce access control.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Build query (RLS will filter to accessible jobs)
    stmt = select(Job).order_by(Job.created_at.desc())

    # Apply status filter
    if status_filter:
        stmt = stmt.where(Job.status == status_filter)

    # Get total count (for pagination)
    count_result = await session.execute(
        select(Job).where(Job.status == status_filter) if status_filter else select(Job)
    )
    total = len(count_result.scalars().all())

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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel job: {str(e)}",
        )


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

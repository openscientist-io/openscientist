"""
Skills management endpoints.

Provides REST API endpoints for listing, searching, and matching skills
to research questions.
"""

import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import get_current_user_from_api_key
from shandy.database.models import Skill, SkillSource, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_session
from shandy.skill_relevance import (
    SkillRelevanceService,
    get_all_categories,
    search_skills,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["Skills"])


# Pydantic models for request/response
class SkillResponse(BaseModel):
    """Response for a skill."""

    id: str = Field(..., description="Skill ID")
    name: str = Field(..., description="Skill name")
    slug: str = Field(..., description="URL-friendly identifier")
    category: str = Field(..., description="Skill category")
    description: Optional[str] = Field(None, description="Brief description")
    tags: List[str] = Field(default_factory=list, description="Skill tags")
    is_enabled: bool = Field(..., description="Whether skill is enabled")
    version: int = Field(..., description="Skill version")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC)")


class SkillDetailResponse(SkillResponse):
    """Detailed response for a skill including full content."""

    content: str = Field(..., description="Full markdown content")
    source_path: Optional[str] = Field(None, description="Source path")


class SkillListResponse(BaseModel):
    """Response for listing skills."""

    skills: List[SkillResponse] = Field(..., description="List of skills")
    total: int = Field(..., description="Total number of skills")


class SkillMatchRequest(BaseModel):
    """Request body for matching skills to a prompt."""

    prompt: str = Field(
        ...,
        min_length=10,
        description="Research question or job description",
        examples=["Analyze metabolomics data from hypothermic samples"],
    )
    max_results: int = Field(
        5,
        ge=1,
        le=20,
        description="Maximum number of skills to return",
    )
    score_threshold: float = Field(
        0.3,
        ge=0.0,
        le=1.0,
        description="Minimum relevance score",
    )


class ScoredSkillResponse(BaseModel):
    """Response for a scored skill match."""

    skill_id: str = Field(..., description="Skill ID")
    name: str = Field(..., description="Skill name")
    category: str = Field(..., description="Skill category")
    slug: str = Field(..., description="URL-friendly identifier")
    description: Optional[str] = Field(None, description="Brief description")
    score: float = Field(..., description="Relevance score (0.0-1.0)")
    match_reason: str = Field(..., description="Explanation of match")


class SkillMatchResponse(BaseModel):
    """Response for skill matching."""

    prompt: str = Field(..., description="Original prompt")
    matches: List[ScoredSkillResponse] = Field(..., description="Matched skills")


class CategoryListResponse(BaseModel):
    """Response for listing categories."""

    categories: List[str] = Field(..., description="List of category names")


class SkillSourceResponse(BaseModel):
    """Response for a skill source."""

    id: str = Field(..., description="Source ID")
    name: str = Field(..., description="Source name")
    source_type: str = Field(..., description="Source type (github/local)")
    url: Optional[str] = Field(None, description="Source URL")
    branch: str = Field(..., description="Git branch")
    is_enabled: bool = Field(..., description="Whether source is enabled")
    last_synced_at: Optional[datetime] = Field(None, description="Last sync timestamp")
    sync_error: Optional[str] = Field(None, description="Last sync error")


def _skill_to_response(skill: Skill) -> SkillResponse:
    """Convert a Skill model to a SkillResponse."""
    return SkillResponse(
        id=str(skill.id),
        name=skill.name,
        slug=skill.slug,
        category=skill.category,
        description=skill.description,
        tags=skill.tags or [],
        is_enabled=skill.is_enabled,
        version=skill.version,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


def _skill_to_detail_response(skill: Skill) -> SkillDetailResponse:
    """Convert a Skill model to a SkillDetailResponse."""
    return SkillDetailResponse(
        id=str(skill.id),
        name=skill.name,
        slug=skill.slug,
        category=skill.category,
        description=skill.description,
        tags=skill.tags or [],
        is_enabled=skill.is_enabled,
        version=skill.version,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        content=skill.content,
        source_path=skill.source_path,
    )


@router.get("", response_model=SkillListResponse)
async def list_skills(
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
    category: Optional[str] = Query(
        None,
        description="Filter by category",
    ),
    search: Optional[str] = Query(
        None,
        description="Full-text search query",
    ),
    tags: Optional[List[str]] = Query(
        None,
        description="Filter by tags (skills must have all specified tags)",
    ),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(50, ge=1, le=100, description="Pagination limit"),
) -> SkillListResponse:
    """
    List skills with optional filtering.

    Supports filtering by category, full-text search, and tags.
    Skills are returned in alphabetical order by name.
    """
    await set_current_user(session, user.id)

    if search:
        # Use full-text search
        skills = await search_skills(
            session,
            query=search,
            category=category,
            tags=tags,
            limit=limit,
        )
        total = len(skills)
    else:
        # Regular query with filters
        conditions = [Skill.is_enabled == True]  # noqa: E712

        if category:
            conditions.append(Skill.category == category)

        if tags:
            for tag in tags:
                conditions.append(Skill.tags.contains([tag]))

        # Get total count
        count_stmt = select(func.count(Skill.id)).where(*conditions)
        count_result = await session.execute(count_stmt)
        total = count_result.scalar() or 0

        # Get paginated results
        stmt = (
            select(Skill)
            .where(*conditions)
            .order_by(Skill.category, Skill.name)
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        skills = list(result.scalars().all())

    return SkillListResponse(
        skills=[_skill_to_response(s) for s in skills],
        total=total,
    )


@router.get("/categories", response_model=CategoryListResponse)
async def list_categories(
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> CategoryListResponse:
    """
    List all skill categories.

    Returns a list of unique category names from all enabled skills.
    """
    await set_current_user(session, user.id)
    categories = await get_all_categories(session)
    return CategoryListResponse(categories=categories)


@router.post("/match", response_model=SkillMatchResponse)
async def match_skills(
    request: SkillMatchRequest,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> SkillMatchResponse:
    """
    Find skills relevant to a research prompt.

    Uses a two-stage approach:
    1. Pre-filter using PostgreSQL full-text search
    2. Score candidates using Anthropic API for semantic relevance

    Returns skills sorted by relevance score.
    """
    await set_current_user(session, user.id)

    service = SkillRelevanceService()
    scored_skills = await service.find_relevant_skills(
        session=session,
        prompt=request.prompt,
        score_threshold=request.score_threshold,
        max_results=request.max_results,
    )

    matches = [
        ScoredSkillResponse(
            skill_id=str(s.skill_id),
            name=s.name,
            category=s.category,
            slug=s.slug,
            description=s.description,
            score=s.score,
            match_reason=s.match_reason,
        )
        for s in scored_skills
    ]

    return SkillMatchResponse(
        prompt=request.prompt,
        matches=matches,
    )


@router.get("/{skill_id}", response_model=SkillDetailResponse)
async def get_skill(
    skill_id: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> SkillDetailResponse:
    """
    Get a skill by ID.

    Returns full skill details including content.
    """
    await set_current_user(session, user.id)

    try:
        skill_uuid = UUID(skill_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid skill ID format",
        )

    stmt = select(Skill).where(
        Skill.id == skill_uuid,
        Skill.is_enabled == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    skill = result.scalar_one_or_none()

    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_id} not found",
        )

    return _skill_to_detail_response(skill)


@router.get("/by-slug/{category}/{slug}", response_model=SkillDetailResponse)
async def get_skill_by_slug(
    category: str,
    slug: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> SkillDetailResponse:
    """
    Get a skill by category and slug.

    Returns full skill details including content.
    """
    await set_current_user(session, user.id)

    stmt = select(Skill).where(
        Skill.category == category,
        Skill.slug == slug,
        Skill.is_enabled == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    skill = result.scalar_one_or_none()

    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {category}/{slug} not found",
        )

    return _skill_to_detail_response(skill)


# =============================================================================
# Skill Sources Management
# =============================================================================


class SkillSourceCreate(BaseModel):
    """Request body for creating a skill source."""

    name: str = Field(..., min_length=1, max_length=255, description="Source name")
    source_type: str = Field(
        ...,
        pattern="^(github|local)$",
        description="Source type: 'github' or 'local'",
    )
    url: Optional[str] = Field(
        None,
        description="GitHub repository URL (required for github type)",
    )
    path: Optional[str] = Field(
        None,
        description="Local filesystem path (required for local type)",
    )
    branch: str = Field(
        "main",
        description="Git branch to sync from",
    )
    skills_path: str = Field(
        "",
        description="Subdirectory within repo containing skills",
    )


class SkillSourceListResponse(BaseModel):
    """Response for listing skill sources."""

    sources: List[SkillSourceResponse] = Field(..., description="List of sources")
    total: int = Field(..., description="Total number of sources")


class SyncTriggerResponse(BaseModel):
    """Response for triggering a sync."""

    source_id: str = Field(..., description="Source ID")
    source_name: str = Field(..., description="Source name")
    success: bool = Field(..., description="Whether sync succeeded")
    created: int = Field(0, description="Skills created")
    updated: int = Field(0, description="Skills updated")
    unchanged: int = Field(0, description="Skills unchanged")
    errors: int = Field(0, description="Errors encountered")
    error_message: Optional[str] = Field(None, description="Error message if failed")


def _source_to_response(source: SkillSource) -> SkillSourceResponse:
    """Convert a SkillSource model to a SkillSourceResponse."""
    return SkillSourceResponse(
        id=str(source.id),
        name=source.name,
        source_type=source.source_type,
        url=source.url,
        branch=source.branch,
        is_enabled=source.is_enabled,
        last_synced_at=source.last_synced_at,
        sync_error=source.sync_error,
    )


@router.get("/sources", response_model=SkillSourceListResponse)
async def list_skill_sources(
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> SkillSourceListResponse:
    """
    List all skill sources.

    Requires admin access (enforced by RLS policy).
    """
    await set_current_user(session, user.id)

    # RLS policy enforces admin-only access
    stmt = select(SkillSource).order_by(SkillSource.name)
    result = await session.execute(stmt)
    sources = list(result.scalars().all())

    return SkillSourceListResponse(
        sources=[_source_to_response(s) for s in sources],
        total=len(sources),
    )


@router.post("/sources", response_model=SkillSourceResponse, status_code=status.HTTP_201_CREATED)
async def create_skill_source(
    source_data: SkillSourceCreate,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> SkillSourceResponse:
    """
    Create a new skill source.

    The source will be synced automatically by the background scheduler.
    """
    await set_current_user(session, user.id)

    # Validate source type specific fields
    if source_data.source_type == "github" and not source_data.url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub sources require a 'url' field",
        )
    if source_data.source_type == "local" and not source_data.path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Local sources require a 'path' field",
        )

    # RLS policy enforces admin-only access
    source = SkillSource(
        name=source_data.name,
        source_type=source_data.source_type,
        url=source_data.url,
        path=source_data.path,
        branch=source_data.branch,
        skills_path=source_data.skills_path,
        is_enabled=True,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    logger.info("Created skill source: %s (%s)", source.name, source.source_type)
    return _source_to_response(source)


@router.post("/sources/{source_id}/sync", response_model=SyncTriggerResponse)
async def sync_skill_source_endpoint(
    source_id: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> SyncTriggerResponse:
    """
    Manually trigger a sync for a skill source.

    Requires admin access (enforced by RLS policy).
    """
    await set_current_user(session, user.id)

    # Verify admin access by trying to read the source (RLS will block non-admins)
    try:
        source_uuid = UUID(source_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid source ID format",
        )

    stmt = select(SkillSource).where(SkillSource.id == source_uuid)
    check_result = await session.execute(stmt)
    if not check_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill source {source_id} not found",
        )

    from shandy.skill_scheduler import get_scheduler

    scheduler = get_scheduler()
    result = await scheduler.sync_source_by_id(source_id)

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill source {source_id} not found",
        )

    return SyncTriggerResponse(
        source_id=result.source_id,
        source_name=result.source_name,
        success=result.success,
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        errors=result.errors,
        error_message=result.error_message,
    )


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill_source(
    source_id: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    Delete a skill source.

    This will also delete all skills from this source (cascade).
    """
    await set_current_user(session, user.id)

    try:
        source_uuid = UUID(source_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid source ID format",
        )

    # RLS policy enforces admin-only access
    stmt = select(SkillSource).where(SkillSource.id == source_uuid)
    result = await session.execute(stmt)
    source = result.scalar_one_or_none()

    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill source {source_id} not found",
        )

    await session.delete(source)
    await session.commit()

    logger.info("Deleted skill source: %s", source_id)

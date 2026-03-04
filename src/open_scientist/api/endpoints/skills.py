"""
Skills management endpoints.

Provides REST API endpoints for listing, searching, and matching skills
to research questions.
"""

import logging
from datetime import datetime
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from open_scientist.api.auth import get_current_user_from_api_key
from open_scientist.api.utils import parse_uuid
from open_scientist.database.models import Skill, SkillSource, User
from open_scientist.database.rls import set_current_user
from open_scientist.database.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["Skills"])


CURRENT_USER_DEP = Depends(get_current_user_from_api_key)
SESSION_DEP = Depends(get_session)
SKILL_TAGS_QUERY = Query(
    None,
    description="Filter by tags (skills must have all specified tags)",
)


# Pydantic models for request/response
class SkillResponse(BaseModel):
    """Response for a skill."""

    id: str = Field(..., description="Skill ID")
    name: str = Field(..., description="Skill name")
    slug: str = Field(..., description="URL-friendly identifier")
    category: str = Field(..., description="Skill category")
    description: str | None = Field(None, description="Brief description")
    tags: list[str] = Field(default_factory=list, description="Skill tags")
    is_enabled: bool = Field(..., description="Whether skill is enabled")
    version: int = Field(..., description="Skill version")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC)")


class SkillDetailResponse(SkillResponse):
    """Detailed response for a skill including full content."""

    content: str = Field(..., description="Full markdown content")
    source_path: str | None = Field(None, description="Source path")


class SkillListResponse(BaseModel):
    """Response for listing skills."""

    skills: list[SkillResponse] = Field(..., description="List of skills")
    total: int = Field(..., description="Total number of skills")


class CategoryListResponse(BaseModel):
    """Response for listing categories."""

    categories: list[str] = Field(..., description="List of category names")


class SkillSourceResponse(BaseModel):
    """Response for a skill source."""

    id: str = Field(..., description="Source ID")
    name: str = Field(..., description="Source name")
    source_type: str = Field(..., description="Source type (github/local)")
    url: str | None = Field(None, description="Source URL")
    branch: str = Field(..., description="Git branch")
    is_enabled: bool = Field(..., description="Whether source is enabled")
    last_synced_at: datetime | None = Field(None, description="Last sync timestamp")
    sync_error: str | None = Field(None, description="Last sync error")


class _SkillResponseFields(TypedDict):
    id: str
    name: str
    slug: str
    category: str
    description: str | None
    tags: list[str]
    is_enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


def _skill_to_response(skill: Skill) -> SkillResponse:
    """Convert a Skill model to a SkillResponse."""
    return SkillResponse(**_skill_response_fields(skill))


def _skill_response_fields(skill: Skill) -> _SkillResponseFields:
    """Return common response fields shared by skill response models."""
    return {
        "id": str(skill.id),
        "name": skill.name,
        "slug": skill.slug,
        "category": skill.category,
        "description": skill.description,
        "tags": skill.tags or [],
        "is_enabled": skill.is_enabled,
        "version": skill.version,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
    }


def _skill_to_detail_response(skill: Skill) -> SkillDetailResponse:
    """Convert a Skill model to a SkillDetailResponse."""
    return SkillDetailResponse(
        **_skill_response_fields(skill),
        content=skill.content,
        source_path=skill.source_path,
    )


def _build_skill_filters(category: str | None, tags: list[str] | None) -> list[ColumnElement[bool]]:
    """Build common WHERE conditions for skill list queries."""
    conditions: list[ColumnElement[bool]] = [Skill.is_enabled.is_(True)]
    if category:
        conditions.append(Skill.category == category)
    if tags:
        conditions.extend(Skill.tags.contains([tag]) for tag in tags)
    return conditions


@router.get("", response_model=SkillListResponse)
async def list_skills(
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
    category: str | None = Query(
        None,
        description="Filter by category",
    ),
    search: str | None = Query(
        None,
        description="Full-text search query",
    ),
    tags: list[str] | None = SKILL_TAGS_QUERY,
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
        # Use full-text search with PostgreSQL
        conditions = _build_skill_filters(category, tags)

        # Create tsquery from search terms
        tsquery = func.plainto_tsquery("english", search)
        conditions.append(Skill.search_vector.op("@@")(tsquery))

        count_stmt = select(func.count(Skill.id)).where(*conditions)
        count_result = await session.execute(count_stmt)
        total = count_result.scalar() or 0

        stmt = (
            select(Skill)
            .where(*conditions)
            .order_by(
                func.ts_rank(Skill.search_vector, tsquery).desc(),
                Skill.category,
                Skill.name,
                Skill.id,
            )
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        skills = list(result.scalars().all())
    else:
        # Regular query with filters
        conditions = _build_skill_filters(category, tags)

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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> CategoryListResponse:
    """
    List all skill categories.

    Returns a list of unique category names from all enabled skills.
    """
    await set_current_user(session, user.id)

    # Get unique categories from enabled skills
    stmt = (
        select(Skill.category).where(Skill.is_enabled.is_(True)).distinct().order_by(Skill.category)
    )
    result = await session.execute(stmt)
    categories = [row[0] for row in result.all()]

    return CategoryListResponse(categories=categories)


@router.get("/{skill_id:uuid}", response_model=SkillDetailResponse)
async def get_skill(
    skill_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> SkillDetailResponse:
    """
    Get a skill by ID.

    Returns full skill details including content.
    """
    await set_current_user(session, user.id)

    skill_uuid = parse_uuid(skill_id, "skill_id")

    stmt = select(Skill).where(
        Skill.id == skill_uuid,
        Skill.is_enabled.is_(True),
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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> SkillDetailResponse:
    """
    Get a skill by category and slug.

    Returns full skill details including content.
    """
    await set_current_user(session, user.id)

    stmt = select(Skill).where(
        Skill.category == category,
        Skill.slug == slug,
        Skill.is_enabled.is_(True),
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
    url: str | None = Field(
        None,
        description="GitHub repository URL (required for github type)",
    )
    path: str | None = Field(
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

    sources: list[SkillSourceResponse] = Field(..., description="List of sources")
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
    error_message: str | None = Field(None, description="Error message if failed")


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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> SyncTriggerResponse:
    """
    Manually trigger a sync for a skill source.

    Requires admin access (enforced by RLS policy).
    """
    await set_current_user(session, user.id)

    # Verify admin access by trying to read the source (RLS will block non-admins)
    source_uuid = parse_uuid(source_id, "source_id")

    stmt = select(SkillSource).where(SkillSource.id == source_uuid)
    check_result = await session.execute(stmt)
    if not check_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill source {source_id} not found",
        )

    from open_scientist.skill_scheduler import get_scheduler

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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> None:
    """
    Delete a skill source.

    This will also delete all skills from this source (cascade).
    """
    await set_current_user(session, user.id)

    source_uuid = parse_uuid(source_id, "source_id")

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

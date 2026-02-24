"""
Tests for skills API endpoints.

Tests all skills endpoints: list, get by id, get by slug, match, categories,
and source management.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match

from shandy.api.endpoints.skills import router
from shandy.database.models import Skill, SkillSource, User
from tests.helpers import enable_rls


@pytest.fixture
def app():
    """Create a FastAPI app with the skills router."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/skills")
    return app


def test_sources_route_is_not_shadowed_by_skill_id_route():
    """GET /skills/sources should resolve to the dedicated sources endpoint."""
    scope = {"type": "http", "path": "/skills/sources", "method": "GET"}

    full_matches = []
    for route in router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            full_matches.append(route.path)

    assert full_matches
    assert full_matches[0] == "/skills/sources"


@pytest.fixture
def mock_auth_user(test_user: User):
    """Mock the authentication dependency."""
    with patch(
        "shandy.api.endpoints.skills.get_current_user_from_api_key",
        return_value=test_user,
    ):
        yield test_user


@pytest.fixture
def mock_session(db_session: AsyncSession):
    """Mock the session dependency."""

    async def get_mock_session():
        yield db_session

    with patch("shandy.api.endpoints.skills.get_session", get_mock_session):
        yield db_session


class TestSkillsListEndpoint:
    """Tests for GET /skills endpoint."""

    @pytest.mark.asyncio
    async def test_list_skills_empty(
        self,
        db_session: AsyncSession,
        test_user: User,
    ):
        """Test listing skills when none exist."""
        from shandy.api.endpoints.skills import list_skills
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        response = await list_skills(
            user=test_user,
            session=db_session,
            category=None,
            search=None,
            tags=None,
            offset=0,
            limit=50,
        )

        assert response.total == 0
        assert response.skills == []

    @pytest.mark.asyncio
    async def test_list_skills_with_skills(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test listing skills returns all skills."""
        _ = (test_skill, test_skill2)
        from shandy.api.endpoints.skills import list_skills
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        response = await list_skills(
            user=test_user,
            session=db_session,
            category=None,
            search=None,
            tags=None,
            offset=0,
            limit=50,
        )

        assert response.total == 2
        assert len(response.skills) == 2
        skill_names = {s.name for s in response.skills}
        assert "Metabolomics Analysis" in skill_names
        assert "Genomics Pipeline" in skill_names

    @pytest.mark.asyncio
    async def test_list_skills_with_search(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test searching skills."""
        _ = (test_skill, test_skill2)
        from shandy.api.endpoints.skills import list_skills
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        response = await list_skills(
            user=test_user,
            session=db_session,
            category=None,
            search="genomics pipeline",
            tags=None,
            offset=0,
            limit=50,
        )

        # Should find genomics skill
        assert len(response.skills) >= 1


class TestSkillsGetEndpoint:
    """Tests for GET /skills/{skill_id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_skill_by_id(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill: Skill,
    ):
        """Test getting a skill by ID."""
        from shandy.api.endpoints.skills import get_skill
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        response = await get_skill(
            skill_id=str(test_skill.id),
            user=test_user,
            session=db_session,
        )

        assert response.id == str(test_skill.id)
        assert response.name == test_skill.name
        assert response.content == test_skill.content

    @pytest.mark.asyncio
    async def test_get_skill_not_found(
        self,
        db_session: AsyncSession,
        test_user: User,
    ):
        """Test getting a non-existent skill."""
        from fastapi import HTTPException

        from shandy.api.endpoints.skills import get_skill
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        with pytest.raises(HTTPException) as exc_info:
            await get_skill(
                skill_id=str(uuid4()),
                user=test_user,
                session=db_session,
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_skill_invalid_id(
        self,
        db_session: AsyncSession,
        test_user: User,
    ):
        """Test getting a skill with invalid ID format."""
        from fastapi import HTTPException

        from shandy.api.endpoints.skills import get_skill
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        with pytest.raises(HTTPException) as exc_info:
            await get_skill(
                skill_id="not-a-uuid",
                user=test_user,
                session=db_session,
            )

        assert exc_info.value.status_code == 400


class TestSkillsBySlugEndpoint:
    """Tests for GET /skills/by-slug/{category}/{slug} endpoint."""

    @pytest.mark.asyncio
    async def test_get_skill_by_slug(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill: Skill,
    ):
        """Test getting a skill by category and slug."""
        from shandy.api.endpoints.skills import get_skill_by_slug
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        response = await get_skill_by_slug(
            category=test_skill.category,
            slug=test_skill.slug,
            user=test_user,
            session=db_session,
        )

        assert response.id == str(test_skill.id)
        assert response.category == test_skill.category
        assert response.slug == test_skill.slug

    @pytest.mark.asyncio
    async def test_get_skill_by_slug_not_found(
        self,
        db_session: AsyncSession,
        test_user: User,
    ):
        """Test getting a non-existent skill by slug."""
        from fastapi import HTTPException

        from shandy.api.endpoints.skills import get_skill_by_slug
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        with pytest.raises(HTTPException) as exc_info:
            await get_skill_by_slug(
                category="nonexistent",
                slug="nonexistent",
                user=test_user,
                session=db_session,
            )

        assert exc_info.value.status_code == 404


class TestSkillsCategoriesEndpoint:
    """Tests for GET /skills/categories endpoint."""

    @pytest.mark.asyncio
    async def test_list_categories(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test listing all categories."""
        _ = (test_skill, test_skill2)
        from shandy.api.endpoints.skills import list_categories
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        response = await list_categories(
            user=test_user,
            session=db_session,
        )

        assert "metabolomics" in response.categories
        assert "genomics" in response.categories


class TestSkillSourcesEndpoints:
    """Tests for skill sources management endpoints."""

    @pytest.mark.asyncio
    async def test_list_skill_sources(
        self,
        db_session: AsyncSession,
        test_admin_user: User,
        test_skill_source: SkillSource,
    ):
        """Test listing skill sources (requires admin)."""
        _ = test_skill_source
        from shandy.api.endpoints.skills import list_skill_sources
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_admin_user.id)

        response = await list_skill_sources(
            user=test_admin_user,
            session=db_session,
        )

        assert response.total >= 1
        source_names = {s.name for s in response.sources}
        assert "Test Skills" in source_names

    @pytest.mark.asyncio
    async def test_create_skill_source(
        self,
        db_session: AsyncSession,
        test_admin_user: User,
    ):
        """Test creating a skill source (requires admin)."""
        from shandy.api.endpoints.skills import SkillSourceCreate, create_skill_source
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_admin_user.id)

        source_data = SkillSourceCreate(
            name="New Source",
            source_type="github",
            url="https://github.com/test/new-skills",
            branch="main",
            skills_path="skills",
        )

        response = await create_skill_source(
            source_data=source_data,
            user=test_admin_user,
            session=db_session,
        )

        assert response.name == "New Source"
        assert response.source_type == "github"
        assert response.url == "https://github.com/test/new-skills"

    @pytest.mark.asyncio
    async def test_create_skill_source_missing_url(
        self,
        db_session: AsyncSession,
        test_admin_user: User,
    ):
        """Test creating a GitHub source without URL fails (requires admin)."""
        from fastapi import HTTPException

        from shandy.api.endpoints.skills import SkillSourceCreate, create_skill_source
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_admin_user.id)

        source_data = SkillSourceCreate(
            name="Invalid Source",
            source_type="github",
            # Missing url
        )

        with pytest.raises(HTTPException) as exc_info:
            await create_skill_source(
                source_data=source_data,
                user=test_admin_user,
                session=db_session,
            )

        assert exc_info.value.status_code == 400
        assert "url" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_delete_skill_source(
        self,
        db_session: AsyncSession,
        test_admin_user: User,
    ):
        """Test deleting a skill source (requires admin)."""
        from sqlalchemy import select

        from shandy.api.endpoints.skills import (
            SkillSourceCreate,
            create_skill_source,
            delete_skill_source,
        )
        from shandy.database.rls import set_current_user

        await set_current_user(db_session, test_admin_user.id)

        # Create a source to delete
        source_data = SkillSourceCreate(
            name="Source to Delete",
            source_type="github",
            url="https://github.com/test/delete-me",
        )

        created = await create_skill_source(
            source_data=source_data,
            user=test_admin_user,
            session=db_session,
        )

        # Delete it
        await delete_skill_source(
            source_id=created.id,
            user=test_admin_user,
            session=db_session,
        )

        # Verify deleted
        from uuid import UUID

        stmt = select(SkillSource).where(SkillSource.id == UUID(created.id))
        result = await db_session.execute(stmt)
        source = result.scalar_one_or_none()

        assert source is None

    @pytest.mark.asyncio
    async def test_sync_skill_source(
        self,
        db_session: AsyncSession,
        test_admin_user: User,
        test_skill_source: SkillSource,
    ):
        """Test triggering a sync for a source (requires admin)."""
        from shandy.api.endpoints.skills import sync_skill_source_endpoint
        from shandy.database.rls import set_current_user
        from shandy.skill_scheduler import SyncResult

        await set_current_user(db_session, test_admin_user.id)

        # Mock the scheduler
        mock_result = SyncResult(
            source_id=str(test_skill_source.id),
            source_name=test_skill_source.name,
            success=True,
            created=5,
            updated=2,
            unchanged=10,
            errors=0,
        )

        with patch("shandy.skill_scheduler.get_scheduler") as mock_get_scheduler:
            mock_scheduler = AsyncMock()
            mock_scheduler.sync_source_by_id.return_value = mock_result
            mock_get_scheduler.return_value = mock_scheduler

            response = await sync_skill_source_endpoint(
                source_id=str(test_skill_source.id),
                user=test_admin_user,
                session=db_session,
            )

        assert response.success is True
        assert response.created == 5
        assert response.updated == 2

    @pytest.mark.asyncio
    async def test_list_skill_sources_non_admin_returns_empty(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill_source: SkillSource,
    ):
        """Test that non-admin users get empty list when listing sources."""
        _ = test_skill_source
        from shandy.api.endpoints.skills import list_skill_sources
        from shandy.database.rls import set_current_user

        # Enable RLS before setting user context (superuser bypasses RLS)
        await enable_rls(db_session)

        # test_user is NOT an admin
        await set_current_user(db_session, test_user.id)

        response = await list_skill_sources(
            user=test_user,
            session=db_session,
        )

        # Non-admin sees empty list due to RLS
        assert response.total == 0
        assert response.sources == []

    @pytest.mark.asyncio
    async def test_delete_skill_source_non_admin_returns_404(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill_source: SkillSource,
    ):
        """Test that non-admin users get 404 when deleting a source."""
        from fastapi import HTTPException

        from shandy.api.endpoints.skills import delete_skill_source
        from shandy.database.rls import set_current_user

        # Enable RLS before setting user context (superuser bypasses RLS)
        await enable_rls(db_session)

        # test_user is NOT an admin
        await set_current_user(db_session, test_user.id)

        with pytest.raises(HTTPException) as exc_info:
            await delete_skill_source(
                source_id=str(test_skill_source.id),
                user=test_user,
                session=db_session,
            )

        # Non-admin gets 404 because RLS hides the source
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_sync_skill_source_non_admin_returns_404(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill_source: SkillSource,
    ):
        """Test that non-admin users get 404 when syncing a source."""
        from fastapi import HTTPException

        from shandy.api.endpoints.skills import sync_skill_source_endpoint
        from shandy.database.rls import set_current_user

        # Enable RLS before setting user context (superuser bypasses RLS)
        await enable_rls(db_session)

        # test_user is NOT an admin
        await set_current_user(db_session, test_user.id)

        with pytest.raises(HTTPException) as exc_info:
            await sync_skill_source_endpoint(
                source_id=str(test_skill_source.id),
                user=test_user,
                session=db_session,
            )

        # Non-admin gets 404 because RLS hides the source
        assert exc_info.value.status_code == 404

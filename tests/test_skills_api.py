"""
Tests for skills API endpoints.

Tests all skills endpoints: list, get by id, get by slug, match, categories,
and source management.
"""

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match

from openscientist.api.endpoints.skills import router
from openscientist.database.models import Skill, SkillSource, User
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
        "openscientist.api.endpoints.skills.get_current_user_from_api_key",
        return_value=test_user,
    ):
        yield test_user


@pytest.fixture
def mock_session(db_session: AsyncSession):
    """Mock the session dependency."""

    async def get_mock_session():
        yield db_session

    with patch("openscientist.api.endpoints.skills.get_session", get_mock_session):
        yield db_session


async def _create_searchable_skill(
    db_session: AsyncSession,
    test_skill_source: SkillSource,
    *,
    name: str,
    slug: str,
    category: str = "pagination",
) -> Skill:
    """Create a skill and ensure search_vector is populated for full-text tests."""
    skill = Skill(
        name=name,
        slug=slug,
        category=category,
        description="Pagination search test skill",
        content=f"# {name}\n\nPagination test content.",
        tags=["pagination", "search"],
        source_id=test_skill_source.id,
        source_path=f"{category}/{slug}.md",
        content_hash=uuid4().hex,
        is_enabled=True,
    )
    db_session.add(skill)
    await db_session.commit()

    await db_session.execute(
        text(
            """
            UPDATE skills SET search_vector =
                setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(description, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(category, '')), 'C')
            WHERE id = :skill_id
        """
        ),
        {"skill_id": skill.id},
    )
    await db_session.commit()
    await db_session.refresh(skill)
    return skill


class TestSkillsListEndpoint:
    """Tests for GET /skills endpoint."""

    @pytest.mark.asyncio
    async def test_list_skills_empty(
        self,
        db_session: AsyncSession,
        test_user: User,
    ):
        """Test listing skills when none exist."""
        from openscientist.api.endpoints.skills import list_skills
        from openscientist.database.rls import set_current_user

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
        from openscientist.api.endpoints.skills import list_skills
        from openscientist.database.rls import set_current_user

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
        from openscientist.api.endpoints.skills import list_skills
        from openscientist.database.rls import set_current_user

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

    @pytest.mark.asyncio
    async def test_list_skills_search_applies_offset_and_limit(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill_source: SkillSource,
    ):
        """Test search mode honors pagination offset and limit."""
        from openscientist.api.endpoints.skills import list_skills
        from openscientist.database.rls import set_current_user

        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="A Skill",
            slug="a-skill",
        )
        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="B Skill",
            slug="b-skill",
        )
        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="C Skill",
            slug="c-skill",
        )
        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="D Skill",
            slug="d-skill",
        )

        await set_current_user(db_session, test_user.id)

        response = await list_skills(
            user=test_user,
            session=db_session,
            category=None,
            search="skill",
            tags=None,
            offset=1,
            limit=2,
        )

        assert response.total == 4
        assert [skill.name for skill in response.skills] == ["B Skill", "C Skill"]

    @pytest.mark.asyncio
    async def test_list_skills_search_total_is_full_match_count(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill_source: SkillSource,
    ):
        """Test search mode total reports full match count, not page size."""
        from openscientist.api.endpoints.skills import list_skills
        from openscientist.database.rls import set_current_user

        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="Alpha Skill",
            slug="alpha-skill",
        )
        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="Beta Skill",
            slug="beta-skill",
        )
        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="Gamma Skill",
            slug="gamma-skill",
        )

        await set_current_user(db_session, test_user.id)

        response = await list_skills(
            user=test_user,
            session=db_session,
            category=None,
            search="skill",
            tags=None,
            offset=0,
            limit=1,
        )

        assert len(response.skills) == 1
        assert response.total == 3

    @pytest.mark.asyncio
    async def test_list_skills_search_offset_past_end_returns_empty_page(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill_source: SkillSource,
    ):
        """Test search mode returns empty page with correct total when offset is past end."""
        from openscientist.api.endpoints.skills import list_skills
        from openscientist.database.rls import set_current_user

        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="One Skill",
            slug="one-skill",
        )
        await _create_searchable_skill(
            db_session,
            test_skill_source,
            name="Two Skill",
            slug="two-skill",
        )

        await set_current_user(db_session, test_user.id)

        response = await list_skills(
            user=test_user,
            session=db_session,
            category=None,
            search="skill",
            tags=None,
            offset=10,
            limit=5,
        )

        assert response.total == 2
        assert response.skills == []


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
        from openscientist.api.endpoints.skills import get_skill
        from openscientist.database.rls import set_current_user

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

        from openscientist.api.endpoints.skills import get_skill
        from openscientist.database.rls import set_current_user

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

        from openscientist.api.endpoints.skills import get_skill
        from openscientist.database.rls import set_current_user

        await set_current_user(db_session, test_user.id)

        with pytest.raises(HTTPException) as exc_info:
            await get_skill(
                skill_id="not-a-uuid",
                user=test_user,
                session=db_session,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid skill_id format"


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
        from openscientist.api.endpoints.skills import get_skill_by_slug
        from openscientist.database.rls import set_current_user

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

        from openscientist.api.endpoints.skills import get_skill_by_slug
        from openscientist.database.rls import set_current_user

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
        from openscientist.api.endpoints.skills import list_categories
        from openscientist.database.rls import set_current_user

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
        from openscientist.api.endpoints.skills import list_skill_sources
        from openscientist.database.rls import set_current_user

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
        from openscientist.api.endpoints.skills import SkillSourceCreate, create_skill_source
        from openscientist.database.rls import set_current_user

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

        from openscientist.api.endpoints.skills import SkillSourceCreate, create_skill_source
        from openscientist.database.rls import set_current_user

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

        from openscientist.api.endpoints.skills import (
            SkillSourceCreate,
            create_skill_source,
            delete_skill_source,
        )
        from openscientist.database.rls import set_current_user

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
        tmp_path: Path,
    ):
        """A sync of a local git repository source runs the real ingester and
        persists the skills it finds."""
        import subprocess

        from sqlalchemy import select

        from openscientist.api.endpoints.skills import sync_skill_source_endpoint
        from openscientist.database.rls import set_current_user
        from tests.helpers import fake_admin_session

        # Local git repository with a skills/ subdirectory holding two skills
        repo = tmp_path / "skills-repo"
        (repo / "skills" / "metabolomics").mkdir(parents=True)
        (repo / "skills" / "metabolomics" / "SKILL.md").write_text(
            "---\n"
            "name: Metabolomics Analysis\n"
            "category: metabolomics\n"
            "description: Statistical analysis of metabolomics data\n"
            "tags:\n"
            "  - statistics\n"
            "---\n\n"
            "# Metabolomics Analysis\n\n"
            "Body content for metabolomics.\n"
        )
        (repo / "skills" / "statistics").mkdir()
        (repo / "skills" / "statistics" / "SKILL.md").write_text(
            "---\n"
            "name: Statistics Toolkit\n"
            "category: statistics\n"
            "description: General statistical helpers\n"
            "---\n\n"
            "# Statistics Toolkit\n\n"
            "Body content for statistics.\n"
        )
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "-qm",
                "initial skills",
            ],
            cwd=repo,
            check=True,
        )

        source = SkillSource(
            name="Local Git Skills",
            source_type="local",
            path=str(repo),
            skills_path="skills",
            is_enabled=True,
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        await set_current_user(db_session, test_admin_user.id)

        # The scheduler reads the source through the admin session seam,
        # so point it at the test session for the real sync to write to.
        with patch(
            "openscientist.skill_scheduler.get_admin_session",
            fake_admin_session(db_session),
        ):
            response = await sync_skill_source_endpoint(
                source_id=str(source.id),
                user=test_admin_user,
                session=db_session,
            )

        assert response.success is True
        assert response.created == 2
        assert response.updated == 0
        assert response.unchanged == 0
        assert response.errors == 0

        stmt = select(Skill).where(Skill.source_id == source.id)
        rows = (await db_session.execute(stmt)).scalars().all()
        assert {s.name for s in rows} == {"Metabolomics Analysis", "Statistics Toolkit"}
        assert {s.source_path for s in rows} == {
            "skills/metabolomics/SKILL.md",
            "skills/statistics/SKILL.md",
        }
        assert {s.slug for s in rows} == {"metabolomics", "statistics"}
        assert all("Body content for" in s.content for s in rows)
        # the sync persisted its result on the source row
        assert source.last_synced_at is not None
        assert source.sync_error is None

    @pytest.mark.asyncio
    async def test_list_skill_sources_non_admin_returns_empty(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_skill_source: SkillSource,
    ):
        """Test that non-admin users get empty list when listing sources."""
        _ = test_skill_source
        from openscientist.api.endpoints.skills import list_skill_sources
        from openscientist.database.rls import set_current_user

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

        from openscientist.api.endpoints.skills import delete_skill_source
        from openscientist.database.rls import set_current_user

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

        from openscientist.api.endpoints.skills import sync_skill_source_endpoint
        from openscientist.database.rls import set_current_user

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

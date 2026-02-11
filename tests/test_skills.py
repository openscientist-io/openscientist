"""
Tests for skill database models.

Tests Skill, SkillSource, and JobSkill models including relationships and constraints.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Job, JobSkill, Skill, SkillSource, User
from shandy.database.rls import bypass_rls, set_current_user


@pytest.mark.asyncio
async def test_skill_source_creation(db_session: AsyncSession):
    """Test creating a skill source."""
    async with bypass_rls(db_session):
        source = SkillSource(
            name="Test GitHub Source",
            source_type="github",
            url="https://github.com/example/skills",
            branch="main",
            skills_path="skills/",
            is_enabled=True,
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

    assert isinstance(source.id, UUID)
    assert source.name == "Test GitHub Source"
    assert source.source_type == "github"
    assert source.url == "https://github.com/example/skills"
    assert source.branch == "main"
    assert source.is_enabled is True
    assert source.last_synced_at is None


@pytest.mark.asyncio
async def test_skill_creation(db_session: AsyncSession, test_skill_source: SkillSource):
    """Test creating a skill."""
    async with bypass_rls(db_session):
        skill = Skill(
            name="Data Analysis",
            slug="data-analysis",
            category="data-science",
            description="General data analysis techniques",
            content="# Data Analysis\n\nContent here...",
            tags=["analysis", "statistics"],
            source_id=test_skill_source.id,
            source_path="data-science/analysis.md",
            content_hash="sha256hash123",
            is_enabled=True,
        )
        db_session.add(skill)
        await db_session.commit()
        await db_session.refresh(skill)

    assert isinstance(skill.id, UUID)
    assert skill.name == "Data Analysis"
    assert skill.slug == "data-analysis"
    assert skill.category == "data-science"
    assert skill.tags == ["analysis", "statistics"]
    assert skill.source_id == test_skill_source.id
    assert skill.is_enabled is True
    assert skill.version == 1


@pytest.mark.asyncio
async def test_skill_source_relationship(
    db_session: AsyncSession,
    test_skill_source: SkillSource,
    test_skill: Skill,
):
    """Test skill-source relationship."""
    async with bypass_rls(db_session):
        await db_session.refresh(test_skill_source, ["skills"])
        await db_session.refresh(test_skill, ["source"])

    # Verify bidirectional relationship
    assert len(test_skill_source.skills) >= 1
    assert test_skill.source is not None
    assert test_skill.source.id == test_skill_source.id


@pytest.mark.asyncio
async def test_job_skill_creation(
    db_session: AsyncSession,
    test_job: Job,
    test_skill: Skill,
):
    """Test creating a job-skill association."""
    async with bypass_rls(db_session):
        job_skill = JobSkill(
            job_id=test_job.id,
            skill_id=test_skill.id,
            is_enabled=True,
            relevance_score=0.9,
            match_reason="High relevance to research question",
        )
        db_session.add(job_skill)
        await db_session.commit()
        await db_session.refresh(job_skill)

    assert job_skill.job_id == test_job.id
    assert job_skill.skill_id == test_skill.id
    assert job_skill.is_enabled is True
    assert job_skill.relevance_score == 0.9
    assert job_skill.match_reason == "High relevance to research question"


@pytest.mark.asyncio
async def test_job_skills_relationship(
    db_session: AsyncSession,
    test_job: Job,
    test_skill: Skill,
    test_job_skill: JobSkill,  # noqa: ARG001  # Fixture ensures relationship exists
):
    """Test job-skills relationship via junction table."""
    async with bypass_rls(db_session):
        await db_session.refresh(test_job, ["job_skills"])

    # Verify relationship
    assert len(test_job.job_skills) == 1
    assert test_job.job_skills[0].skill_id == test_skill.id

    # Test convenience property
    skills = test_job.skills
    assert len(skills) == 1
    assert skills[0].id == test_skill.id


@pytest.mark.asyncio
async def test_skill_cascade_delete(
    db_session: AsyncSession,
    test_skill_source: SkillSource,
    test_skill: Skill,
):
    """Test that deleting a source cascades to skills."""
    skill_id = test_skill.id

    async with bypass_rls(db_session):
        # Delete source
        await db_session.delete(test_skill_source)
        await db_session.commit()

        # Verify skill is deleted
        stmt = select(Skill).where(Skill.id == skill_id)
        result = await db_session.execute(stmt)
        skill = result.scalar_one_or_none()
        assert skill is None


@pytest.mark.asyncio
async def test_job_skill_cascade_delete(
    db_session: AsyncSession,
    test_job: Job,
    test_skill: Skill,
    test_job_skill: JobSkill,  # noqa: ARG001  # Fixture ensures relationship exists
):
    """Test that deleting a job cascades to job_skills."""
    job_id = test_job.id
    skill_id = test_skill.id

    async with bypass_rls(db_session):
        # Delete job
        await db_session.delete(test_job)
        await db_session.commit()

        # Verify job_skill is deleted
        stmt = select(JobSkill).where(
            JobSkill.job_id == job_id,
            JobSkill.skill_id == skill_id,
        )
        result = await db_session.execute(stmt)
        job_skill = result.scalar_one_or_none()
        assert job_skill is None


@pytest.mark.asyncio
async def test_skill_rls_public_read(
    db_session: AsyncSession,
    test_user: User,
    test_skill: Skill,
):
    """Test that authenticated users can read enabled skills."""
    # Set user context (not bypass)
    await set_current_user(db_session, test_user.id)

    # Query skill
    stmt = select(Skill).where(Skill.id == test_skill.id)
    result = await db_session.execute(stmt)
    skill = result.scalar_one_or_none()

    assert skill is not None
    assert skill.id == test_skill.id


@pytest.mark.asyncio
async def test_skill_unique_category_slug(
    db_session: AsyncSession,
    test_skill_source: SkillSource,  # noqa: ARG001  # Required for test_skill fixture
    test_skill: Skill,
):
    """Test unique constraint on (category, slug)."""
    from sqlalchemy.exc import IntegrityError

    async with bypass_rls(db_session):
        # Try to create a skill with same category and slug
        duplicate_skill = Skill(
            name="Different Name",
            slug=test_skill.slug,  # Same slug
            category=test_skill.category,  # Same category
            description="Different description",
            content="Different content",
            tags=[],
            content_hash="different_hash",
            is_enabled=True,
        )
        db_session.add(duplicate_skill)

        with pytest.raises(IntegrityError):
            await db_session.commit()

        await db_session.rollback()


@pytest.mark.asyncio
async def test_skill_version_increment(
    db_session: AsyncSession,
    test_skill: Skill,
):
    """Test skill version tracking."""
    assert test_skill.version == 1

    async with bypass_rls(db_session):
        # Update skill
        test_skill.content = "Updated content"
        test_skill.content_hash = "new_hash"
        test_skill.version = test_skill.version + 1
        await db_session.commit()
        await db_session.refresh(test_skill)

    assert test_skill.version == 2

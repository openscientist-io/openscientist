"""
Tests for skill database models.

Tests Skill, SkillSource, and JobSkill models including relationships and constraints.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Skill, SkillSource, User
from shandy.database.rls import bypass_rls, set_current_user


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

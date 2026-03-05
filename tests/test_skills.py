"""
Tests for skill database models.

Tests Skill and SkillSource models including relationships and constraints.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Skill, SkillSource, User
from openscientist.database.rls import set_current_user


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
    test_skill_source: SkillSource,  # Required for test_skill fixture
    test_skill: Skill,
):
    """Test unique constraint on (category, slug)."""
    _ = test_skill_source
    from sqlalchemy.exc import IntegrityError

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

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate_skill)
            await db_session.flush()


@pytest.mark.asyncio
async def test_skill_version_increment(
    db_session: AsyncSession,
    test_skill: Skill,
):
    """Test skill version tracking."""
    assert test_skill.version == 1

    # Update skill
    test_skill.content = "Updated content"
    test_skill.content_hash = "new_hash"
    test_skill.version = test_skill.version + 1
    await db_session.commit()
    await db_session.refresh(test_skill)

    assert test_skill.version == 2

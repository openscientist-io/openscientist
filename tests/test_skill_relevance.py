"""
Tests for skill relevance matching service.

Tests full-text search pre-filtering and semantic scoring with mocked Anthropic API.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Skill
from shandy.skill_relevance import (
    get_all_categories,
    get_skills_by_category,
    search_skills,
)


@pytest.mark.asyncio
async def test_get_all_categories(
    db_session: AsyncSession,
    test_skill: Skill,
    test_skill2: Skill,
):
    """Test getting all unique categories."""
    categories = await get_all_categories(db_session)

    assert "metabolomics" in categories
    assert "genomics" in categories
    assert len(categories) == 2


@pytest.mark.asyncio
async def test_get_skills_by_category(
    db_session: AsyncSession,
    test_skill: Skill,
    test_skill2: Skill,
):
    """Test getting skills by category."""
    metabolomics_skills = await get_skills_by_category(db_session, "metabolomics")
    genomics_skills = await get_skills_by_category(db_session, "genomics")
    empty_skills = await get_skills_by_category(db_session, "nonexistent")

    assert len(metabolomics_skills) == 1
    assert metabolomics_skills[0].name == "Metabolomics Analysis"

    assert len(genomics_skills) == 1
    assert genomics_skills[0].name == "Genomics Pipeline"

    assert len(empty_skills) == 0


@pytest.mark.asyncio
async def test_search_skills_full_text(
    db_session: AsyncSession,
    test_skill: Skill,
    test_skill2: Skill,
):
    """Test full-text search on skills."""
    # Search for "metabolomics"
    results = await search_skills(db_session, "metabolomics analysis")

    assert len(results) >= 1
    assert any(s.name == "Metabolomics Analysis" for s in results)


@pytest.mark.asyncio
async def test_search_skills_with_category_filter(
    db_session: AsyncSession,
    test_skill: Skill,
    test_skill2: Skill,
):
    """Test search with category filter."""
    # Search in metabolomics category only
    results = await search_skills(
        db_session,
        "analysis pipeline",
        category="metabolomics",
    )

    # Should only find metabolomics skills
    for result in results:
        assert result.category == "metabolomics"


@pytest.mark.asyncio
async def test_search_skills_with_tags_filter(
    db_session: AsyncSession,
    test_skill: Skill,
):
    """Test search with tags filter."""
    # Search with tag filter
    results = await search_skills(
        db_session,
        "analysis",
        tags=["statistics"],
    )

    # Should only find skills with "statistics" tag
    assert len(results) >= 1
    for result in results:
        assert "statistics" in result.tags

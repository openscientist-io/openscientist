"""
Tests for skill relevance matching service.

Tests full-text search pre-filtering and semantic scoring with mocked Anthropic API.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Skill, SkillSource
from shandy.database.rls import bypass_rls, set_current_user
from shandy.skill_relevance import (
    ScoredSkill,
    SkillRelevanceService,
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
    async with bypass_rls(db_session):
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
    async with bypass_rls(db_session):
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
    async with bypass_rls(db_session):
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
    async with bypass_rls(db_session):
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
    async with bypass_rls(db_session):
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


class TestSkillRelevanceService:
    """Tests for SkillRelevanceService."""

    @pytest.mark.asyncio
    async def test_prefilter_skills(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test pre-filtering skills using full-text search."""
        service = SkillRelevanceService()

        async with bypass_rls(db_session):
            candidates = await service._prefilter_skills(
                db_session,
                "metabolomics statistical analysis",
                limit=10,
            )

        assert len(candidates) >= 1
        # Metabolomics skill should be in results
        assert any(c.name == "Metabolomics Analysis" for c in candidates)

    @pytest.mark.asyncio
    async def test_fallback_scoring(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test fallback text similarity scoring."""
        service = SkillRelevanceService(api_key=None)  # No API key = use fallback

        # Create mock skills for scoring
        candidates = [test_skill, test_skill2]

        scored = service._fallback_scoring(
            "metabolomics analysis statistics",
            candidates,
        )

        assert len(scored) == 2
        # All should have scores
        for s in scored:
            assert 0.0 <= s.score <= 1.0
            assert s.match_reason == "Matched by text similarity"

        # Metabolomics skill should score higher for metabolomics query
        metabolomics_score = next(
            s.score for s in scored if s.name == "Metabolomics Analysis"
        )
        genomics_score = next(s.score for s in scored if s.name == "Genomics Pipeline")
        assert metabolomics_score > genomics_score

    @pytest.mark.asyncio
    async def test_score_skills_batch_with_mocked_api(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test batch scoring with mocked Anthropic API."""
        service = SkillRelevanceService(api_key="test-key")

        # Mock the anthropic client
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text='{"scores": [{"skill_num": 1, "score": 0.9, "reason": "High match"}, {"skill_num": 2, "score": 0.3, "reason": "Low match"}]}'
            )
        ]

        candidates = [test_skill, test_skill2]

        with patch("shandy.skill_relevance.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_anthropic.Anthropic.return_value = mock_client

            scored = await service._score_skills_batch(
                "metabolomics analysis",
                candidates,
            )

        assert len(scored) == 2
        # First skill should have high score
        assert scored[0].score == 0.9
        assert scored[0].match_reason == "High match"
        # Second skill should have low score
        assert scored[1].score == 0.3

    @pytest.mark.asyncio
    async def test_find_relevant_skills_end_to_end(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test full relevance matching flow with fallback scoring."""
        # Use service without API key to use fallback
        service = SkillRelevanceService(api_key=None)

        async with bypass_rls(db_session):
            results = await service.find_relevant_skills(
                session=db_session,
                prompt="metabolomics data analysis statistical methods",
                candidate_limit=10,
                score_threshold=0.0,  # Include all for testing
                max_results=5,
            )

        # Should have results
        assert len(results) >= 1

        # Results should be ScoredSkill objects
        for result in results:
            assert isinstance(result, ScoredSkill)
            assert isinstance(result.score, float)
            assert 0.0 <= result.score <= 1.0

    @pytest.mark.asyncio
    async def test_find_relevant_skills_score_threshold(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test that score threshold filters low-scoring skills."""
        service = SkillRelevanceService(api_key=None)

        async with bypass_rls(db_session):
            # High threshold - may exclude low-scoring skills
            results = await service.find_relevant_skills(
                session=db_session,
                prompt="metabolomics analysis",
                score_threshold=0.8,  # Very high threshold
                max_results=10,
            )

        # Results should only include high-scoring skills
        for result in results:
            assert result.score >= 0.8

    @pytest.mark.asyncio
    async def test_find_relevant_skills_max_results(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
        test_skill2: Skill,
    ):
        """Test that max_results limits output."""
        service = SkillRelevanceService(api_key=None)

        async with bypass_rls(db_session):
            results = await service.find_relevant_skills(
                session=db_session,
                prompt="analysis",
                score_threshold=0.0,
                max_results=1,
            )

        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_api_fallback_on_error(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
    ):
        """Test fallback to text similarity when API fails."""
        service = SkillRelevanceService(api_key="test-key")

        with patch("shandy.skill_relevance.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.side_effect = Exception("API Error")

            scored = await service._score_skills_batch(
                "test query",
                [test_skill],
            )

        # Should fall back to text similarity
        assert len(scored) == 1
        assert scored[0].match_reason == "Matched by text similarity"

    @pytest.mark.asyncio
    async def test_api_json_parsing_with_code_blocks(
        self,
        db_session: AsyncSession,
        test_skill: Skill,
    ):
        """Test handling of API response with markdown code blocks."""
        service = SkillRelevanceService(api_key="test-key")

        # API sometimes returns JSON wrapped in code blocks
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text='```json\n{"scores": [{"skill_num": 1, "score": 0.7, "reason": "Good match"}]}\n```'
            )
        ]

        with patch("shandy.skill_relevance.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_anthropic.Anthropic.return_value = mock_client

            scored = await service._score_skills_batch(
                "test query",
                [test_skill],
            )

        assert len(scored) == 1
        assert scored[0].score == 0.7

"""Tests for prompts module."""

import hashlib
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Skill
from shandy.prompts import (
    build_discovery_prompt,
    format_skills_content,
    format_skills_list,
    get_relevant_skills,
    get_system_prompt,
)


class TestGetSystemPrompt:
    """Tests for system prompt generation."""

    def test_skills_enabled_mentions_search_skills(self):
        prompt = get_system_prompt(skills_enabled=True)
        assert "search_skills" in prompt
        assert "hypothesis-generation" in prompt

    def test_skills_disabled_no_search_skills(self):
        prompt = get_system_prompt(skills_enabled=False)
        assert "search_skills" not in prompt
        assert "hypothesis-generation" not in prompt

    def test_both_prompts_mention_execute_code(self):
        for skills in (True, False):
            prompt = get_system_prompt(skills_enabled=skills)
            assert "execute_code" in prompt
            assert "search_pubmed" in prompt

    def test_both_prompts_mention_principles(self):
        for skills in (True, False):
            prompt = get_system_prompt(skills_enabled=skills)
            assert "effect sizes" in prompt
            assert "Negative results" in prompt


class TestBuildDiscoveryPrompt:
    """Tests for discovery prompt construction."""

    def test_first_iteration_shows_first_iteration_guidance(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=1,
            max_iterations=20,
        )
        assert "first iteration" in prompt.lower()
        assert "Iteration 1/20" in prompt

    def test_early_iteration_shows_exploration_phase(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=3,
            max_iterations=20,
        )
        assert "early exploration phase" in prompt.lower()

    def test_middle_iteration_shows_deep_investigation(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=8,
            max_iterations=20,
        )
        assert "deep investigation phase" in prompt.lower()

    def test_late_iteration_shows_approaching_limit(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=18,
            max_iterations=20,
        )
        assert "approaching the iteration limit" in prompt.lower()

    def test_skills_option_shown_when_provided(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=1,
            max_iterations=10,
            skills_available="- hypothesis-generation\n- result-interpretation",
        )
        assert "Option E: Use Skill" in prompt
        assert "hypothesis-generation" in prompt

    def test_skills_option_hidden_when_not_provided(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=1,
            max_iterations=10,
        )
        assert "Option E" not in prompt

    def test_knowledge_graph_summary_included(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## KG: Found 5 findings",
            iteration=5,
            max_iterations=20,
        )
        assert "KG: Found 5 findings" in prompt

    def test_save_iteration_summary_reminder_at_end(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=1,
            max_iterations=10,
        )
        assert "save_iteration_summary" in prompt


class TestFormatSkillsList:
    """Tests for skills list formatting."""

    def test_empty_skills(self):
        assert format_skills_list({}) == ""

    def test_single_skill(self):
        skills = {"hypothesis-generation": {"description": "Generate hypotheses"}}
        result = format_skills_list(skills)
        assert "hypothesis-generation" in result
        assert "Generate hypotheses" in result
        assert "Available skills:" in result

    def test_multiple_skills(self):
        skills = {
            "hypothesis-generation": {"description": "Generate hypotheses"},
            "result-interpretation": {"description": "Interpret results"},
        }
        result = format_skills_list(skills)
        assert "hypothesis-generation" in result
        assert "result-interpretation" in result

    def test_missing_description(self):
        skills = {"my-skill": {}}
        result = format_skills_list(skills)
        assert "No description" in result


class TestFormatSkillsContent:
    """Tests for format_skills_content function."""

    def test_empty_skills(self):
        result = format_skills_content([])
        assert result == ""

    def test_single_skill(self):
        skill = MagicMock(spec=Skill)
        skill.name = "Test Skill"
        skill.category = "test-category"
        skill.description = "A test skill"
        skill.content = "# Skill Content\nThis is the content."

        result = format_skills_content([skill])
        assert "Domain-Specific Skills" in result
        assert "Test Skill" in result
        assert "test-category" in result.lower()
        assert "A test skill" in result
        assert "Skill Content" in result

    def test_multiple_categories(self):
        skill1 = MagicMock(spec=Skill)
        skill1.name = "Skill A"
        skill1.category = "alpha"
        skill1.description = "Skill A desc"
        skill1.content = "Content A"

        skill2 = MagicMock(spec=Skill)
        skill2.name = "Skill B"
        skill2.category = "beta"
        skill2.description = "Skill B desc"
        skill2.content = "Content B"

        result = format_skills_content([skill1, skill2])
        assert "Alpha Skills" in result
        assert "Beta Skills" in result
        assert "Skill A" in result
        assert "Skill B" in result


class TestGetRelevantSkills:
    """Tests for get_relevant_skills function (requires database)."""

    @pytest.fixture
    async def skill_fixture(self, db_session: AsyncSession) -> Skill:
        """Create a test skill with search vector."""
        from sqlalchemy import text

        skill = Skill(
            name="Metabolomics Analysis",
            slug="metabolomics-analysis",
            category="data-science",
            description="Statistical analysis of metabolomics data",
            content="# Metabolomics Analysis\n\nGuide for analyzing metabolomics data.",
            tags=["metabolomics", "statistics"],
            content_hash=hashlib.sha256(b"test").hexdigest(),
            is_enabled=True,
        )
        db_session.add(skill)
        await db_session.commit()

        # Update search vector manually (normally done by trigger)
        await db_session.execute(
            text(
                """
                UPDATE skills SET search_vector = to_tsvector('english',
                    coalesce(name, '') || ' ' || coalesce(description, '') || ' ' || coalesce(content, ''))
                WHERE id = :id
                """
            ),
            {"id": str(skill.id)},
        )
        await db_session.commit()
        await db_session.refresh(skill)
        return skill

    async def test_finds_matching_skill(self, db_session: AsyncSession, skill_fixture: Skill):
        """Test that full-text search finds matching skills."""
        skills = await get_relevant_skills(
            db_session,
            "metabolomics statistical analysis",
            limit=5,
        )
        assert len(skills) >= 1
        assert any(s.name == "Metabolomics Analysis" for s in skills)

    async def test_falls_back_to_all_enabled(self, db_session: AsyncSession):
        """Test fallback when no search matches."""
        # Create a skill with no matching search terms
        skill = Skill(
            name="Unrelated Skill",
            slug="unrelated",
            category="misc",
            description="Something unrelated",
            content="Content here",
            tags=[],
            content_hash=hashlib.sha256(b"unrelated").hexdigest(),
            is_enabled=True,
        )
        db_session.add(skill)
        await db_session.commit()

        # Search for something that won't match
        skills = await get_relevant_skills(
            db_session,
            "xyzzy quantum flux capacitor",
            limit=5,
        )
        # Should fall back to enabled skills
        assert len(skills) >= 1

    async def test_respects_limit(self, db_session: AsyncSession):
        """Test that limit parameter is respected."""
        # Create multiple skills
        for i in range(10):
            skill = Skill(
                name=f"Test Skill {i}",
                slug=f"test-skill-{i}",
                category="test",
                description=f"Test description {i}",
                content=f"Test content {i}",
                tags=[],
                content_hash=hashlib.sha256(f"test{i}".encode()).hexdigest(),
                is_enabled=True,
            )
            db_session.add(skill)
        await db_session.commit()

        skills = await get_relevant_skills(db_session, "test", limit=3)
        assert len(skills) <= 3

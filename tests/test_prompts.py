"""Tests for prompts module."""

from shandy.prompts import (
    build_discovery_prompt,
    format_skills_list,
    get_system_prompt,
)


class TestGetSystemPrompt:
    """Tests for system prompt generation."""

    def test_skills_enabled_mentions_claude_skills_dir(self):
        prompt = get_system_prompt(skills_enabled=True)
        assert ".claude/skills/" in prompt

    def test_skills_disabled_no_claude_skills_dir(self):
        prompt = get_system_prompt(skills_enabled=False)
        assert ".claude/skills/" not in prompt

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

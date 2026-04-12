"""Tests for prompts module."""

import re

from claude_agent_sdk.types import AgentDefinition

from openscientist.prompts import (
    build_discovery_prompt,
    format_skills_list,
    get_system_prompt,
)

_EXPECTED_EXPERT_SLUGS: list[str] = [
    "research-lead",
    "research-subagent",
    "citations-agent",
    "data-scientist",
    "python-pro",
    "scientific-literature-researcher",
    "data-researcher",
    "research-analyst",
]


def _canned_experts() -> dict[str, AgentDefinition]:
    """Build a small, deterministic expert set for prompt tests."""
    return {
        "alpha": AgentDefinition(
            description="Use for task type A",
            prompt="you are alpha",
        ),
        "beta": AgentDefinition(
            description="Use for task type B",
            prompt="you are beta",
        ),
    }


class TestGetSystemPrompt:
    """Tests for system prompt generation."""

    def test_mentions_claude_skills_dir(self):
        prompt = get_system_prompt()
        assert ".claude/skills/" in prompt

    def test_mentions_execute_code(self):
        prompt = get_system_prompt()
        assert "execute_code" in prompt
        assert "search_pubmed" in prompt

    def test_mentions_principles(self):
        prompt = get_system_prompt()
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
        assert "Option F: Use Skill" in prompt
        assert "hypothesis-generation" in prompt

    def test_skills_option_hidden_when_not_provided(self):
        prompt = build_discovery_prompt(
            knowledge_graph_summary="## Summary",
            iteration=1,
            max_iterations=10,
        )
        assert "Option F" not in prompt

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


class TestSystemPromptExpertDelegation:
    """Tests for the Expert Delegation section of get_system_prompt()."""

    def test_no_delegation_section_when_experts_is_none(self) -> None:
        """No experts means no delegation section in the prompt."""
        prompt = get_system_prompt()
        assert "Expert Delegation" not in prompt
        for slug in _EXPECTED_EXPERT_SLUGS:
            assert slug not in prompt

    def test_delegation_section_reflects_supplied_experts(self) -> None:
        """Supplied experts dict renders a delegation section."""
        experts = _canned_experts()
        prompt = get_system_prompt(experts=experts)
        assert "Expert Delegation" in prompt
        for slug, agent_def in experts.items():
            assert slug in prompt, f"supplied slug {slug!r} missing"
            assert agent_def.description in prompt

    def test_unregistered_slug_not_in_rendered_prompt(self) -> None:
        """Unsupplied slugs do not appear in the prompt."""
        experts = _canned_experts()
        prompt = get_system_prompt(experts=experts)
        for stale_slug in _EXPECTED_EXPERT_SLUGS:
            assert stale_slug not in prompt

    def test_empty_experts_dict_omits_delegation_section(self) -> None:
        """An empty dict is equivalent to None — no delegation section."""
        prompt = get_system_prompt(experts={})
        assert "Expert Delegation" not in prompt

    def test_delegation_language_present_when_experts_supplied(self) -> None:
        experts = _canned_experts()
        prompt = get_system_prompt(experts=experts).lower()
        assert "delegate" in prompt

    def test_delegation_section_length_is_bounded(self) -> None:
        """Delegation section stays within a sensible size range."""
        experts = _canned_experts()
        prompt = get_system_prompt(experts=experts)
        match = re.search(
            r"(?im)^##\s*expert delegation\s*$(.+?)(?=^##\s|\Z)",
            prompt,
            re.DOTALL | re.MULTILINE,
        )
        assert match is not None
        section = match.group(1).strip()
        assert 100 <= len(section) <= 3000, (
            f"Expert Delegation section length is {len(section)} chars; expected 100 <= N <= 3000"
        )

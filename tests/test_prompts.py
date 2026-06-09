"""Tests for prompts module."""

from openscientist.prompts import (
    build_discovery_prompt,
    format_skills_list,
    generate_job_agents_md,
    generate_job_claude_md,
    get_system_prompt,
)


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

    def test_claude_backend_explicit_matches_default(self):
        assert get_system_prompt(agent_backend="claude_code") == get_system_prompt()

    def test_codex_backend_drops_claude_paths(self):
        prompt = get_system_prompt(agent_backend="codex")
        assert ".claude/" not in prompt
        # the shared, backend-agnostic body is still present
        assert "execute_code" in prompt
        assert "effect sizes" in prompt


class TestBackendJobDocs:
    """The Claude (CLAUDE.md) and Codex (AGENTS.md) per-job docs."""

    def test_claude_doc_keeps_claude_vocabulary(self):
        doc = generate_job_claude_md(use_hypotheses=True, phenix_available=True)
        assert ".claude/skills/" in doc
        assert "Claude's built-in `Read` tool" in doc

    def test_codex_doc_drops_claude_vocabulary(self):
        doc = generate_job_agents_md(use_hypotheses=True, phenix_available=True)
        assert ".claude/" not in doc
        assert "Claude's" not in doc
        # shared body intact (a hypothesis tool, a core tool)
        assert "add_hypothesis" in doc
        assert "save_iteration_summary" in doc

    def test_docs_route_tabular_to_execute_code(self):
        """Tabular analysis must go through execute_code with the
        data/data_files contract, not a file reader, so the model stops
        guessing host-style paths inside the executor."""
        for doc in (
            generate_job_claude_md(use_hypotheses=False, phenix_available=False),
            generate_job_agents_md(use_hypotheses=False, phenix_available=False),
        ):
            # The execute_code data-access contract is spelled out.
            assert 'pd.read_csv(data_files[i]["path"])' in doc
            assert "data_files" in doc
            # Tabular files are no longer routed to a file reader.
            assert "CSV, TSV, TXT, JSON| Claude's built-in `Read` tool" not in doc
            # Explicit warning against guessing executor paths.
            assert "do not exist in this executor" in doc

    def test_docs_mandate_visualization_with_show(self):
        """Findings should be visualized, and the prompt must specify the
        plt.show() save pattern the executor captures, not a manual savefig."""
        for doc in (
            generate_job_claude_md(use_hypotheses=False, phenix_available=False),
            generate_job_agents_md(use_hypotheses=False, phenix_available=False),
        ):
            assert "Visualize every quantitative finding" in doc
            assert "plt.show()" in doc
            assert "do not call `plt.savefig()`" in doc

    def test_codex_doc_respects_use_hypotheses_flag(self):
        with_h = generate_job_agents_md(use_hypotheses=True)
        without_h = generate_job_agents_md(use_hypotheses=False)
        assert "add_hypothesis" in with_h
        assert "add_hypothesis" not in without_h


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

"""Tests for prompts module."""

from types import SimpleNamespace

from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.codex_agent import CodexAgent
from openscientist.prompts import (
    build_discovery_prompt,
    format_skills_list,
    generate_job_claude_md,
)


class TestSystemPrompt:
    """Tests for each backend's system prompt (AbstractAgent.system_prompt)."""

    def test_mentions_claude_skills_dir(self):
        prompt = ClaudeCodeAgent.system_prompt()
        assert ".claude/skills/" in prompt

    def test_mentions_execute_code(self):
        prompt = ClaudeCodeAgent.system_prompt()
        assert "execute_code" in prompt
        assert "search_pubmed" in prompt

    def test_mentions_principles(self):
        prompt = ClaudeCodeAgent.system_prompt()
        assert "effect sizes" in prompt
        assert "Negative results" in prompt

    def test_backends_produce_distinct_prompts(self):
        assert ClaudeCodeAgent.system_prompt() != CodexAgent.system_prompt()

    def test_codex_backend_drops_claude_paths(self):
        prompt = CodexAgent.system_prompt()
        assert ".claude/" not in prompt
        # the shared, backend-agnostic body is still present
        assert "execute_code" in prompt
        assert "effect sizes" in prompt


class TestBackendJobDocs:
    """The Claude (CLAUDE.md) and Codex (AGENTS.md) per-job docs."""

    def test_claude_doc_keeps_claude_vocabulary(self):
        doc = generate_job_claude_md(phenix_available=True)
        assert ".claude/skills/" in doc
        assert "Claude's built-in `Read` tool" in doc

    def test_codex_doc_drops_claude_vocabulary(self):
        doc = CodexAgent.job_doc(phenix_available=True)
        assert ".claude/" not in doc
        assert "Claude's" not in doc
        # shared body intact (two core tools)
        assert "update_knowledge_state" in doc
        assert "save_iteration_summary" in doc

    def test_docs_route_tabular_to_execute_code(self):
        """Tabular analysis must go through execute_code with the
        data/data_files contract, not a file reader, so the model stops
        guessing host-style paths inside the executor."""
        for doc in (
            generate_job_claude_md(phenix_available=False),
            CodexAgent.job_doc(phenix_available=False),
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
            generate_job_claude_md(phenix_available=False),
            CodexAgent.job_doc(phenix_available=False),
        ):
            assert "Visualize every quantitative finding" in doc
            assert "plt.show()" in doc
            assert "do not call `plt.savefig()`" in doc

    def test_codex_doc_has_no_phantom_skills_refs(self):
        """Codex gets skills via its own ## Skills injection, so the codex prompt
        must not name the nonexistent search_skills tool or a phantom skills dir.
        The Claude path keeps search_skills (it has the .claude/skills files)."""
        for txt in (
            CodexAgent.job_doc(phenix_available=True),
            CodexAgent.system_prompt(),
        ):
            assert "search_skills" not in txt
            assert "skills/` directory provided to you" not in txt
        assert "search_skills" in generate_job_claude_md(phenix_available=True)

    def test_docs_never_mention_removed_hypothesis_tooling(self):
        """The hypothesis-tracking opt-in was removed: no backend doc may teach
        the retired tools, the retired skill, or a hypothesis scoring formula."""
        for doc in (
            generate_job_claude_md(phenix_available=True),
            CodexAgent.job_doc(phenix_available=True),
        ):
            assert "add_hypothesis" not in doc
            assert "update_hypothesis" not in doc
            assert "hypothesis-generation" not in doc
            assert "Hypothesis Tracking" not in doc


class TestAirgapSearchNote:
    """The search_pubmed doc gains AND-semantics guidance only when air-gapped,
    and never leaks the raw placeholder."""

    @staticmethod
    def _set_airgap(monkeypatch, enabled):
        monkeypatch.setattr(
            "openscientist.settings.get_settings",
            lambda: SimpleNamespace(airgap=SimpleNamespace(enabled=enabled)),
        )

    def test_online_docs_omit_note_and_placeholder(self, monkeypatch):
        self._set_airgap(monkeypatch, False)
        for doc in (
            generate_job_claude_md(phenix_available=True),
            CodexAgent.job_doc(phenix_available=True),
        ):
            assert "{{AIRGAP_SEARCH_NOTE}}" not in doc
            assert "This run is air-gapped" not in doc
            assert "Every term is required (AND)" not in doc

    def test_airgap_docs_add_note_to_both_backends(self, monkeypatch):
        self._set_airgap(monkeypatch, True)
        for doc in (
            generate_job_claude_md(phenix_available=True),
            CodexAgent.job_doc(phenix_available=True),
        ):
            assert "{{AIRGAP_SEARCH_NOTE}}" not in doc
            assert "This run is air-gapped" in doc
            assert "Every term is required (AND)" in doc
            assert (
                doc.index("**search_pubmed**")
                < doc.index("This run is air-gapped")
                < doc.index("**update_knowledge_state**")
            )


class TestRenderChatContext:
    """The job-chat context must honor backend fragments, sharing the discovery
    substitution path so the chat agent is never told about tools/paths its
    backend lacks."""

    def test_claude_chat_context_is_identity(self):
        from openscientist.prompts.claude import CLAUDE_FRAGMENTS
        from openscientist.prompts.common import read_chat_template, render_chat_context

        # Claude fragments are identity substitutions: the rendered context is
        # the packaged template verbatim.
        assert render_chat_context(CLAUDE_FRAGMENTS) == read_chat_template()

    def test_codex_chat_context_drops_claude_vocabulary(self):
        ctx = CodexAgent.chat_doc()
        assert "Claude's" not in ctx
        assert "`.claude/skills/`" not in ctx
        # The guidance itself is preserved.
        assert "execute_code" in ctx


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
            skills_available="- prioritization\n- result-interpretation",
        )
        assert "Option F: Use Skill" in prompt
        assert "prioritization" in prompt

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
        skills = {"prioritization": {"description": "Decide what to do next"}}
        result = format_skills_list(skills)
        assert "prioritization" in result
        assert "Decide what to do next" in result
        assert "Available skills:" in result

    def test_multiple_skills(self):
        skills = {
            "prioritization": {"description": "Decide what to do next"},
            "result-interpretation": {"description": "Interpret results"},
        }
        result = format_skills_list(skills)
        assert "prioritization" in result
        assert "result-interpretation" in result

    def test_missing_description(self):
        skills = {"my-skill": {}}
        result = format_skills_list(skills)
        assert "No description" in result

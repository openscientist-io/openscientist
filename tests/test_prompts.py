"""Tests for prompts module."""

from types import SimpleNamespace

import pytest

from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.codex_agent import CodexAgent
from openscientist.agent.omp_agent import OmpAgent
from openscientist.prompts import generate_job_claude_md


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
        doc = generate_job_claude_md(use_hypotheses=True, phenix_available=True)
        assert ".claude/skills/" in doc
        assert "Claude's built-in `Read` tool" in doc

    def test_codex_doc_drops_claude_vocabulary(self):
        doc = CodexAgent.job_doc(use_hypotheses=True, phenix_available=True)
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
            CodexAgent.job_doc(use_hypotheses=False, phenix_available=False),
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
            CodexAgent.job_doc(use_hypotheses=False, phenix_available=False),
        ):
            assert "Visualize every quantitative finding" in doc
            assert "plt.show()" in doc
            assert "do not call `plt.savefig()`" in doc

    def test_codex_doc_has_no_phantom_skills_dir(self):
        """Codex gets skills via its own ## Skills injection, so the codex prompt
        must not point at a skills directory it does not have."""
        for txt in (
            CodexAgent.job_doc(use_hypotheses=True, phenix_available=True),
            CodexAgent.system_prompt(),
        ):
            assert "skills/` directory provided to you" not in txt

    @pytest.mark.parametrize("agent", [ClaudeCodeAgent, CodexAgent, OmpAgent])
    def test_no_backend_names_the_unregistered_search_skills_tool(self, agent):
        """No MCP server registers a search_skills tool, so no prompt may send an
        agent to it."""
        for txt in (
            agent.job_doc(use_hypotheses=True, phenix_available=True),
            agent.job_doc(use_hypotheses=False, phenix_available=False),
            agent.system_prompt(),
        ):
            assert "search_skills" not in txt

    def test_codex_doc_respects_use_hypotheses_flag(self):
        with_h = CodexAgent.job_doc(use_hypotheses=True)
        without_h = CodexAgent.job_doc(use_hypotheses=False)
        assert "add_hypothesis" in with_h
        assert "add_hypothesis" not in without_h


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
            generate_job_claude_md(use_hypotheses=True, phenix_available=True),
            CodexAgent.job_doc(use_hypotheses=True, phenix_available=True),
        ):
            assert "{{AIRGAP_SEARCH_NOTE}}" not in doc
            assert "This run is air-gapped" not in doc
            assert "Every term is required (AND)" not in doc

    def test_airgap_docs_add_note_to_both_backends(self, monkeypatch):
        self._set_airgap(monkeypatch, True)
        for doc in (
            generate_job_claude_md(use_hypotheses=True, phenix_available=True),
            CodexAgent.job_doc(use_hypotheses=True, phenix_available=True),
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

    def test_codex_chat_context_drops_claude_vocabulary(self):
        ctx = CodexAgent.chat_doc()
        assert "Claude's" not in ctx
        assert "`.claude/skills/`" not in ctx
        # The guidance itself is preserved.
        assert "execute_code" in ctx


_AGENTS = (ClaudeCodeAgent, CodexAgent, OmpAgent)
_EM_DASH = "\u2014"


def _report_prompt() -> str:
    from openscientist.knowledge_state import KnowledgeState
    from openscientist.orchestrator.iteration import build_report_prompt

    return build_report_prompt(
        "Does X bind Y?", KnowledgeState("job", "Does X bind Y?", 3), context_window_tokens=131072
    )


class TestWritingStyle:
    """Every model turn that writes text for the scientist carries the writing
    rules, and no prompt models the em dash the rules forbid."""

    @pytest.mark.parametrize("agent", _AGENTS)
    def test_the_discovery_doc_carries_the_rules(self, agent) -> None:
        from openscientist.prompts.common import WRITING_STYLE

        doc = agent.job_doc(use_hypotheses=True, phenix_available=True)
        assert WRITING_STYLE in doc
        assert "{{WRITING_STYLE}}" not in doc

    @pytest.mark.parametrize("agent", _AGENTS)
    def test_the_chat_doc_carries_the_rules(self, agent) -> None:
        from openscientist.prompts.common import WRITING_STYLE

        doc = agent.chat_doc()
        assert WRITING_STYLE in doc
        assert "{{WRITING_STYLE}}" not in doc

    def test_the_report_prompt_carries_the_rules(self) -> None:
        from openscientist.prompts.common import WRITING_STYLE

        assert WRITING_STYLE in _report_prompt()

    def test_the_rules_cover_text_figures_and_the_em_dash(self) -> None:
        from openscientist.prompts.common import WRITING_STYLE

        assert "U+2014" in WRITING_STYLE
        for figure_text in ("titles", "axis labels", "legends"):
            assert figure_text in WRITING_STYLE

    @pytest.mark.parametrize("agent", _AGENTS)
    def test_no_prompt_uses_an_em_dash(self, agent) -> None:
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.orchestrator.iteration import (
            build_consensus_prompt,
            build_initial_prompt,
            build_iteration_prompt,
            build_report_retry_prompt,
        )

        ks = KnowledgeState("job", "Does X bind Y?", 3)
        prompts = {
            "system": agent.system_prompt(),
            "discovery doc": agent.job_doc(use_hypotheses=True, phenix_available=True),
            "chat doc": agent.chat_doc(),
            "initial": build_initial_prompt("Q", 3, ["d.csv"], ks, tool_prefix=""),
            "iteration": build_iteration_prompt(2, 3, ks, "More on Y", tool_prefix=""),
            "report": _report_prompt(),
            "report retry": build_report_retry_prompt("Q", ks, context_window_tokens=131072),
            "consensus": build_consensus_prompt("Q", tool_prefix=""),
        }
        for name, prompt in prompts.items():
            assert _EM_DASH not in prompt, f"the {name} prompt uses an em dash"

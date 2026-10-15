"""Tests for job_detail page helpers: stat badges, analysis-log metadata, plot
collection, and the read-only rendering of hypotheses recorded by jobs that
ran while the (since removed) hypothesis-tracking opt-in existed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import (
    _analysis_log_meta_lines,
    _collect_iteration_plots,
    _stats_badges,
)

# ---------------------------------------------------------------------------
# _stats_badges
# ---------------------------------------------------------------------------


class TestStatsBadges:
    """_stats_badges reports counts plus agent/provider/model identity."""

    def _get_labels(self, badges: list) -> list[str]:
        return [b[0] for b in badges]

    def _get_badge(self, badges: list, label: str) -> tuple | None:
        return next((b for b in badges if b[0] == label), None)

    def _badge_value(self, badges: list, label: str) -> Any:
        badge = self._get_badge(badges, label)
        return badge[1] if badge is not None else None

    def _make_job(
        self,
        status: str = "completed",
        findings: int = 0,
        llm_provider: str | None = None,
        llm_model: str | None = None,
    ) -> Any:
        return SimpleNamespace(
            status=JobStatus(status),
            iterations_completed=2,
            max_iterations=5,
            findings_count=findings,
            llm_provider=llm_provider,
            llm_model=llm_model,
        )

    def test_no_hypotheses_badge_when_count_is_zero(self) -> None:
        # New jobs record no hypotheses, so the badge is absent for them.
        badges = _stats_badges(self._make_job(), lit_count=3, hyp_count=0)
        assert "Hypotheses" not in self._get_labels(badges)

    def test_legacy_hypotheses_badge_appears_when_count_nonzero(self) -> None:
        # Jobs that ran with the removed opt-in still show their stored count.
        badges = _stats_badges(self._make_job(), lit_count=3, hyp_count=5)
        badge = self._get_badge(badges, "Hypotheses")
        assert badge is not None
        assert badge[1] == 5
        assert badge[2] == "orange"

    def test_findings_badge_present_alongside_legacy_hypotheses(self) -> None:
        badges = _stats_badges(self._make_job(findings=4), lit_count=2, hyp_count=3)
        labels = self._get_labels(badges)
        assert "Findings" in labels
        assert "Hypotheses" in labels

    def test_default_hyp_count_zero_omits_badge(self) -> None:
        badges = _stats_badges(self._make_job(), lit_count=0)
        assert "Hypotheses" not in self._get_labels(badges)

    def test_codex_job_shows_agent_and_provider_not_model(self) -> None:
        # Codex stores no model id, so the badge must read the agent + provider,
        # not the misleading "Model: Openai".
        badges = _stats_badges(self._make_job(llm_provider="openai"), lit_count=0)
        assert self._badge_value(badges, "Agent") == "Codex"
        assert self._badge_value(badges, "Provider") == "OpenAI API"
        assert self._get_badge(badges, "Model") is None

    def test_azure_openai_job_shows_codex_and_azure_provider(self) -> None:
        badges = _stats_badges(self._make_job(llm_provider="azure-openai"), lit_count=0)
        assert self._badge_value(badges, "Agent") == "Codex"
        assert self._badge_value(badges, "Provider") == "Azure OpenAI Service"
        assert self._get_badge(badges, "Model") is None

    def test_ollama_job_shows_codex_and_ollama_provider(self) -> None:
        badges = _stats_badges(self._make_job(llm_provider="ollama"), lit_count=0)
        assert self._badge_value(badges, "Agent") == "Codex"
        assert self._badge_value(badges, "Provider") == "Ollama (local)"

    def test_ollama_job_with_model_shows_provider_and_model(self) -> None:
        badges = _stats_badges(
            self._make_job(llm_provider="ollama", llm_model="gpt-oss:20b"), lit_count=0
        )
        # Provider (where it is hosted) and model (which one ran) appear together.
        assert self._badge_value(badges, "Agent") == "Codex"
        assert self._badge_value(badges, "Provider") == "Ollama (local)"
        assert self._badge_value(badges, "Model") == "gpt-oss:20b"

    def test_vllm_job_shows_omp_and_vllm_provider(self) -> None:
        badges = _stats_badges(
            self._make_job(llm_provider="vllm", llm_model="Qwen/Qwen3-32B"), lit_count=0
        )
        # vLLM is not a Codex backend, so the badge names the harness that drives it.
        assert self._badge_value(badges, "Agent") == "Oh My Pi"
        assert self._badge_value(badges, "Provider") == "vLLM (self-hosted)"
        assert self._badge_value(badges, "Model") == "Qwen/Qwen3-32B"

    def test_llamacpp_job_shows_omp_and_llamacpp_provider(self) -> None:
        badges = _stats_badges(
            self._make_job(llm_provider="llamacpp", llm_model="meta-llama/Llama-3.1-8B-Instruct"),
            lit_count=0,
        )
        # llama.cpp is not a Codex backend, so the badge names the harness that drives it.
        assert self._badge_value(badges, "Agent") == "Oh My Pi"
        assert self._badge_value(badges, "Provider") == "llama.cpp (self-hosted)"
        assert self._badge_value(badges, "Model") == "meta-llama/Llama-3.1-8B-Instruct"

    def test_claude_job_shows_agent_and_model(self) -> None:
        badges = _stats_badges(
            self._make_job(llm_provider="anthropic", llm_model="claude-sonnet-4-5-20250929"),
            lit_count=0,
        )
        assert self._badge_value(badges, "Agent") == "Claude Code"
        assert self._badge_value(badges, "Model") == "Claude Sonnet 4.5"
        # Provider and model are independent badges shown together.
        assert self._badge_value(badges, "Provider") == "Anthropic"

    def test_no_provider_omits_agent_badge(self) -> None:
        badges = _stats_badges(self._make_job(), lit_count=0)
        labels = self._get_labels(badges)
        assert "Agent" not in labels
        assert "Provider" not in labels

    def test_model_badge_uses_human_readable_label(self) -> None:
        badges = _stats_badges(
            self._make_job(llm_provider="anthropic", llm_model="claude-sonnet-4-5-20250929"),
            lit_count=3,
        )
        badge = self._get_badge(badges, "Model")
        assert badge is not None
        assert badge[1] == "Claude Sonnet 4.5"
        assert badge[2] == "cyan"

    def test_missing_model_shows_provider_badge_not_model(self) -> None:
        badges = _stats_badges(
            self._make_job(llm_provider="anthropic", llm_model=None),
            lit_count=1,
        )
        assert self._get_badge(badges, "Model") is None
        assert self._badge_value(badges, "Provider") == "Anthropic"


# ---------------------------------------------------------------------------
# _analysis_log_meta_lines
# ---------------------------------------------------------------------------


class TestAnalysisLogMetadata:
    """Analysis log helpers should surface concise metadata for the timeline UI."""

    def test_search_pubmed_metadata_includes_query_and_result_count(self) -> None:
        lines = _analysis_log_meta_lines(
            {
                "action": "search_pubmed",
                "query": "hypothermia metabolomics",
                "results_count": 7,
            }
        )

        assert [line.text for line in lines] == [
            'Query: "hypothermia metabolomics"',
            "Papers found: 7",
        ]
        assert all(line.italic is False for line in lines)

    def test_legacy_hypothesis_entry_still_renders(self) -> None:
        """Analysis logs written by the retired add/update_hypothesis tools
        must keep rendering for jobs that ran while the opt-in existed."""
        lines = _analysis_log_meta_lines(
            {
                "action": "update_hypothesis",
                "statement": "Cold exposure shifts the metabolome.",
                "status": "supported",
                "result_summary": "Observed in the treated cohort.",
            }
        )

        assert [line.text for line in lines] == [
            "Cold exposure shifts the metabolome.",
            "Status: supported",
            "Observed in the treated cohort.",
        ]
        assert [line.italic for line in lines] == [True, False, False]

    def test_execute_code_metadata_adds_duration(self) -> None:
        lines = _analysis_log_meta_lines({"action": "execute_code", "execution_time": 3.5})
        assert [line.text for line in lines] == ["Duration: 3.5s"]


# ---------------------------------------------------------------------------
# _collect_iteration_plots
# ---------------------------------------------------------------------------


class TestCollectIterationPlots:
    """Plot collection should skip unreadable metadata files instead of crashing."""

    def test_skips_non_utf8_metadata_files(self, tmp_path: Path) -> None:
        plots_dir = tmp_path / "provenance"
        plots_dir.mkdir()
        (plots_dir / "plot.png").write_bytes(b"png")
        (plots_dir / "plot.json").write_bytes(b"\xff\xfe\x00\x00")

        assert _collect_iteration_plots(plots_dir, 1) == []


# ---------------------------------------------------------------------------
# Legacy hypotheses: per-iteration filtering and counting (read-only display)
# ---------------------------------------------------------------------------


def _hypotheses_for_iteration(ks_data: dict, iter_num: int) -> list[dict]:
    """The filter shared by _render_iteration_hypotheses and _render_iteration_card."""
    return [
        h
        for h in ks_data.get("hypotheses", [])
        if h.get("iteration_proposed") == iter_num or h.get("iteration_tested") == iter_num
    ]


class TestLegacyIterationHypothesesFiltering:
    """Stored hypotheses appear under the iteration that proposed or tested them;
    knowledge states without any (every new job) yield nothing."""

    def _proposed_in(self, iter_num: int, status: str = "pending") -> dict:
        return {
            "id": f"H{iter_num:03d}",
            "statement": f"Hypothesis proposed in iter {iter_num}",
            "status": status,
            "iteration_proposed": iter_num,
            "result": {},
        }

    def _tested_in(self, iter_num: int, status: str = "supported") -> dict:
        return {
            "id": f"T{iter_num:03d}",
            "statement": f"Hypothesis tested in iter {iter_num}",
            "status": status,
            "iteration_proposed": 1,
            "iteration_tested": iter_num,
            "result": {"summary": "Confirmed", "conclusion": "Yes."},
        }

    def test_proposed_this_iter_included(self) -> None:
        result = _hypotheses_for_iteration({"hypotheses": [self._proposed_in(2)]}, 2)
        assert len(result) == 1
        assert result[0]["statement"] == "Hypothesis proposed in iter 2"

    def test_tested_this_iter_included(self) -> None:
        assert len(_hypotheses_for_iteration({"hypotheses": [self._tested_in(3)]}, 3)) == 1

    def test_other_iter_excluded(self) -> None:
        ks = {"hypotheses": [self._proposed_in(1), self._proposed_in(3)]}
        assert _hypotheses_for_iteration(ks, 2) == []

    def test_proposed_and_tested_same_iter_both_included(self) -> None:
        ks = {"hypotheses": [self._proposed_in(2), self._tested_in(2)]}
        assert len(_hypotheses_for_iteration(ks, 2)) == 2

    def test_empty_hypotheses_list_returns_empty(self) -> None:
        assert _hypotheses_for_iteration({"hypotheses": []}, 1) == []

    def test_no_hypotheses_key_returns_empty(self) -> None:
        assert _hypotheses_for_iteration({}, 1) == []

    def test_all_statuses_included_regardless(self) -> None:
        hypotheses = [
            {**self._proposed_in(1), "status": s}
            for s in ("pending", "testing", "supported", "rejected")
        ]
        assert len(_hypotheses_for_iteration({"hypotheses": hypotheses}, 1)) == 4

    def test_count_correct_for_mixed_hypotheses(self) -> None:
        timeline_ks = {
            "hypotheses": [
                {"iteration_proposed": 1},
                {"iteration_proposed": 2, "iteration_tested": 3},
                {"iteration_proposed": 3},
            ]
        }
        assert len(_hypotheses_for_iteration(timeline_ks, 1)) == 1
        assert len(_hypotheses_for_iteration(timeline_ks, 2)) == 1
        assert len(_hypotheses_for_iteration(timeline_ks, 3)) == 2

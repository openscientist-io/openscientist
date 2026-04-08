"""Tests for knowledge_state module."""

import pytest

from openscientist.knowledge_state import KnowledgeState, _sanitize_for_json


@pytest.fixture
def ks() -> KnowledgeState:
    """Create a fresh KnowledgeState for testing."""
    return KnowledgeState(
        job_id="test_job",
        research_question="What drives metabolite X?",
        max_iterations=20,
        use_skills=True,
    )


class TestKnowledgeStateInit:
    """Tests for initialization."""

    def test_initial_iteration_is_one(self, ks):
        assert ks.data["iteration"] == 1

    def test_config_stored(self, ks):
        cfg = ks.data["config"]
        assert cfg["job_id"] == "test_job"
        assert cfg["research_question"] == "What drives metabolite X?"
        assert cfg["max_iterations"] == 20
        assert cfg["use_skills"] is True

    def test_empty_collections(self, ks):
        assert ks.data["hypotheses"] == []
        assert ks.data["findings"] == []
        assert ks.data["literature"] == []
        assert ks.data["analysis_log"] == []
        assert ks.data["iteration_summaries"] == []
        assert ks.data["feedback_history"] == []


class TestHypotheses:
    """Tests for hypothesis CRUD."""

    def test_add_hypothesis_returns_id(self, ks):
        hid = ks.add_hypothesis("X correlates with Y")
        assert hid == "H001"

    def test_sequential_ids(self, ks):
        h1 = ks.add_hypothesis("First")
        h2 = ks.add_hypothesis("Second")
        h3 = ks.add_hypothesis("Third")
        assert h1 == "H001"
        assert h2 == "H002"
        assert h3 == "H003"

    def test_hypothesis_initial_status(self, ks):
        ks.add_hypothesis("Test hyp")
        hyp = ks.data["hypotheses"][0]
        assert hyp["status"] == "pending"
        assert hyp["proposed_by"] == "agent"
        assert hyp["iteration_proposed"] == 1

    def test_update_hypothesis(self, ks):
        hid = ks.add_hypothesis("Test hyp")
        ks.update_hypothesis(hid, {"status": "supported", "result": {"p": 0.01}})
        hyp = ks.data["hypotheses"][0]
        assert hyp["status"] == "supported"
        assert hyp["result"]["p"] == 0.01

    def test_update_nonexistent_raises(self, ks):
        with pytest.raises(ValueError, match="not found"):
            ks.update_hypothesis("H999", {"status": "rejected"})


class TestFindings:
    """Tests for finding CRUD."""

    def test_add_finding_returns_id(self, ks):
        fid = ks.add_finding("Discovery 1", "p < 0.01, d = 0.8")
        assert fid == "F001"

    def test_sequential_finding_ids(self, ks):
        f1 = ks.add_finding("First", "evidence1")
        f2 = ks.add_finding("Second", "evidence2")
        assert f1 == "F001"
        assert f2 == "F002"

    def test_finding_optional_fields(self, ks):
        ks.add_finding(
            "Discovery",
            "evidence",
            supporting_hypotheses=["H001"],
            literature_support=["L001"],
            plots=["plot_1.png"],
        )
        finding = ks.data["findings"][0]
        assert finding["supporting_hypotheses"] == ["H001"]
        assert finding["literature_support"] == ["L001"]
        assert finding["plots"] == ["plot_1.png"]

    def test_finding_defaults(self, ks):
        ks.add_finding("Discovery", "evidence")
        finding = ks.data["findings"][0]
        assert finding["supporting_hypotheses"] == []
        assert finding["plots"] == []
        assert finding["citations"] == []

    def test_finding_with_citations(self, ks):
        ks.add_literature("12345678", "A Paper", "mHTT aggregates were observed in 89% of neurons")
        ks.add_finding(
            "HTT aggregation",
            "p < 0.001",
            citations=[
                {
                    "pmid": "12345678",
                    "snippet": "mHTT aggregates were observed in 89% of neurons",
                    "explanation": "Demonstrates aggregation",
                }
            ],
        )
        finding = ks.data["findings"][0]
        assert len(finding["citations"]) == 1
        assert finding["citations"][0]["validation_status"] == "verified"
        assert finding["citations"][0]["pmid"] == "12345678"

    def test_finding_without_citations_backwards_compatible(self, ks):
        ks.add_finding("Old finding", "some evidence")
        finding = ks.data["findings"][0]
        assert finding["citations"] == []


class TestValidateCitation:
    """Tests for citation validation."""

    def test_verified_exact_match(self, ks):
        ks.add_literature("111", "Paper", "The protein was detected in all samples.")
        result = ks.validate_citation({"pmid": "111", "snippet": "detected in all samples"})
        assert result["validation_status"] == "verified"

    def test_verified_normalized(self, ks):
        ks.add_literature("222", "Paper", "The  protein\nwas   detected.")
        result = ks.validate_citation({"pmid": "222", "snippet": "the protein was detected."})
        assert result["validation_status"] == "verified_normalized"

    def test_mismatch(self, ks):
        ks.add_literature("333", "Paper", "The protein was detected.")
        result = ks.validate_citation({"pmid": "333", "snippet": "completely unrelated text"})
        assert result["validation_status"] == "mismatch"

    def test_unchecked_pmid_not_found(self, ks):
        result = ks.validate_citation({"pmid": "999", "snippet": "some text"})
        assert result["validation_status"] == "unchecked"

    def test_empty_snippet_is_mismatch(self, ks):
        ks.add_literature("444", "Paper", "Some abstract text.")
        result = ks.validate_citation({"pmid": "444", "snippet": ""})
        assert result["validation_status"] == "mismatch"


class TestLiterature:
    """Tests for literature reference tracking."""

    def test_add_literature_returns_id(self, ks):
        lid = ks.add_literature("12345", "A paper", "Abstract text")
        assert lid == "L001"

    def test_literature_fields(self, ks):
        ks.add_literature(
            pmid="99999",
            title="Important paper",
            abstract="We found...",
            relevance_to=["H001"],
            search_query="metabolomics hypothermia",
        )
        lit = ks.data["literature"][0]
        assert lit["pmid"] == "99999"
        assert lit["relevance_to"] == ["H001"]
        assert lit["search_query"] == "metabolomics hypothermia"


class TestAnalysisLog:
    """Tests for analysis logging."""

    def test_log_analysis_basic(self, ks):
        ks.log_analysis("execute_code", code="print('hi')", output="hi")
        assert len(ks.data["analysis_log"]) == 1
        entry = ks.data["analysis_log"][0]
        assert entry["action"] == "execute_code"
        assert entry["code"] == "print('hi')"
        assert entry["iteration"] == 1

    def test_log_analysis_extra_kwargs(self, ks):
        ks.log_analysis("search_pubmed", query="cancer", results=5)
        entry = ks.data["analysis_log"][0]
        assert entry["query"] == "cancer"
        assert entry["results"] == 5


class TestIterationSummaries:
    """Tests for iteration summary tracking."""

    def test_add_and_get_summary(self, ks):
        ks.add_iteration_summary(1, "Explored the data", strapline="Data exploration")
        result = ks.get_iteration_summary(1)
        assert result == "Explored the data"

    def test_update_existing_summary(self, ks):
        ks.add_iteration_summary(1, "First version")
        ks.add_iteration_summary(1, "Updated version")
        result = ks.get_iteration_summary(1)
        assert result == "Updated version"

    def test_get_nonexistent_returns_none(self, ks):
        assert ks.get_iteration_summary(99) is None

    def test_increment_iteration(self, ks):
        assert ks.data["iteration"] == 1
        ks.increment_iteration()
        assert ks.data["iteration"] == 2


class TestFeedback:
    """Tests for scientist feedback system."""

    def test_add_and_get_feedback(self, ks):
        ks.add_feedback("Focus on pathway X", after_iteration=3)
        result = ks.get_feedback_for_iteration(4)  # feedback for iter 4 = after iter 3
        assert result == "Focus on pathway X"

    def test_no_feedback_returns_none(self, ks):
        assert ks.get_feedback_for_iteration(1) is None

    def test_multiple_feedback_returns_latest(self, ks):
        ks.add_feedback("First note", after_iteration=2)
        ks.add_feedback("Second note", after_iteration=2)
        result = ks.get_feedback_for_iteration(3)
        assert result == "Second note"


class TestGetSummary:
    """Tests for the text summary generation."""

    def test_includes_research_question(self, ks):
        summary = ks.get_summary()
        assert "What drives metabolite X?" in summary

    def test_includes_iteration(self, ks):
        summary = ks.get_summary()
        assert "Iteration 1" in summary

    def test_includes_findings(self, ks):
        ks.add_finding("Key discovery", "p=0.001")
        summary = ks.get_summary()
        assert "Key discovery" in summary
        assert "Findings confirmed: 1" in summary

    def test_includes_pending_hypotheses(self, ks):
        ks.add_hypothesis("X causes Y")
        summary = ks.get_summary()
        assert "X causes Y" in summary
        assert "Pending Hypotheses" in summary

    def test_includes_rejected_hypotheses(self, ks):
        hid = ks.add_hypothesis("Bad idea")
        ks.update_hypothesis(hid, {"status": "rejected", "result": {"conclusion": "No effect"}})
        summary = ks.get_summary()
        assert "Rejected Hypotheses" in summary
        assert "Bad idea" in summary


class TestVersionInfo:
    """Tests for version info metadata."""

    def test_set_version_info(self, ks):
        ks.set_version_info({"claude_model": "claude-4", "openscientist_commit": "abc123"})
        assert ks.data["config"]["version_info"]["claude_model"] == "claude-4"


class TestGetReportSummary:
    """Tests for the comprehensive report summary generation."""

    def test_includes_all_findings_not_just_last_three(self, ks):
        for i in range(5):
            ks.add_finding(f"Finding {i + 1}", f"evidence-{i + 1}")
        summary = ks.get_report_summary()
        for i in range(5):
            assert f"Finding {i + 1}" in summary
            assert f"evidence-{i + 1}" in summary

    def test_includes_supported_hypotheses_with_results(self, ks):
        hid = ks.add_hypothesis("Gene X is upregulated")
        ks.update_hypothesis(
            hid,
            {
                "status": "supported",
                "result": {
                    "summary": "Significant upregulation observed",
                    "p_value": "p=0.003",
                    "effect_size": "Cohen's d=1.2",
                    "conclusion": "Strong evidence for upregulation",
                },
            },
        )
        summary = ks.get_report_summary()
        assert "Gene X is upregulated" in summary
        assert "Supported Hypotheses" in summary
        assert "p=0.003" in summary
        assert "Cohen's d=1.2" in summary
        assert "Strong evidence for upregulation" in summary

    def test_includes_rejected_hypotheses_with_conclusions(self, ks):
        hid = ks.add_hypothesis("Protein Y is degraded")
        ks.update_hypothesis(
            hid,
            {
                "status": "rejected",
                "result": {"conclusion": "No degradation observed"},
            },
        )
        summary = ks.get_report_summary()
        assert "Rejected Hypotheses" in summary
        assert "Protein Y is degraded" in summary
        assert "No degradation observed" in summary

    def test_includes_pending_hypotheses_as_knowledge_gaps(self, ks):
        ks.add_hypothesis("Untested idea")
        summary = ks.get_report_summary()
        assert "Knowledge Gaps" in summary
        assert "Untested idea" in summary

    def test_includes_iteration_summaries_timeline(self, ks):
        ks.add_iteration_summary(1, "Explored data structure", strapline="Data exploration")
        ks.add_iteration_summary(2, "Tested correlation hypothesis")
        summary = ks.get_report_summary()
        assert "Investigation Timeline" in summary
        assert "Explored data structure" in summary
        assert "Tested correlation hypothesis" in summary
        assert "Data exploration" in summary

    def test_includes_literature_with_truncated_abstracts(self, ks):
        long_abstract = "A" * 300
        ks.add_literature("12345", "Important paper", long_abstract, relevance_to=["H001"])
        summary = ks.get_report_summary()
        assert "Literature Reviewed" in summary
        assert "Important paper" in summary
        assert "PMID: 12345" in summary
        # Abstract should be truncated to 200 chars + "..."
        assert "A" * 200 + "..." in summary
        assert "A" * 201 not in summary.split("...")[0]
        assert "Relevant to: H001" in summary

    def test_excludes_analysis_log(self, ks):
        ks.log_analysis("execute_code", code="import pandas", output="success")
        summary = ks.get_report_summary()
        assert "import pandas" not in summary

    def test_includes_progress_overview_counts(self, ks):
        ks.add_hypothesis("H one")
        h2 = ks.add_hypothesis("H two")
        ks.update_hypothesis(h2, {"status": "supported", "result": {}})
        h3 = ks.add_hypothesis("H three")
        ks.update_hypothesis(h3, {"status": "rejected", "result": {}})
        ks.add_finding("F one", "ev")

        summary = ks.get_report_summary()
        assert "Total hypotheses proposed: 3" in summary
        assert "Supported: 1" in summary
        assert "Rejected: 1" in summary
        assert "Pending: 1" in summary
        assert "Findings confirmed: 1" in summary

    def test_handles_empty_state(self, ks):
        summary = ks.get_report_summary()
        assert "Research Question" in summary
        assert "What drives metabolite X?" in summary
        # Should not crash and should have progress counts of 0
        assert "Total hypotheses proposed: 0" in summary
        assert "Findings confirmed: 0" in summary

    def test_includes_consensus_answer_if_set(self, ks):
        ks.data["consensus_answer"] = "The answer is 42."
        summary = ks.get_report_summary()
        assert "Previous Consensus Answer" in summary
        assert "The answer is 42." in summary

    def test_no_consensus_section_when_unset(self, ks):
        summary = ks.get_report_summary()
        assert "Previous Consensus Answer" not in summary

    def test_finding_optional_fields_included(self, ks):
        ks.add_finding(
            "Complex finding",
            "p<0.001",
            supporting_hypotheses=["H001", "H002"],
            literature_support=["L001"],
            plots=["volcano_plot.png"],
        )
        # Set interpretation
        ks.data["findings"][0]["biological_interpretation"] = "Pathway activation"
        summary = ks.get_report_summary()
        assert "Pathway activation" in summary
        assert "H001, H002" in summary
        assert "L001" in summary
        assert "volcano_plot.png" in summary


class TestAgentStatus:
    """Tests for agent status management."""

    def test_set_agent_status(self, ks):
        ks.set_agent_status("Analyzing data distribution")
        assert ks.data["agent_status"] == "Analyzing data distribution"
        assert ks.data["agent_status_updated_at"] is not None

    def test_clear_agent_status(self, ks):
        ks.set_agent_status("Working...")
        ks.clear_agent_status()
        assert ks.data["agent_status"] is None
        assert ks.data["agent_status_updated_at"] is None

    def test_get_agent_status(self, ks):
        assert ks.get_agent_status() is None
        ks.set_agent_status("Searching PubMed")
        assert ks.get_agent_status() == "Searching PubMed"

    def test_get_agent_status_not_set(self, ks):
        assert ks.get_agent_status() is None


class TestIncrementIteration:
    """Tests for increment_iteration standalone."""

    def test_increments(self, ks):
        assert ks.data["iteration"] == 1
        ks.increment_iteration()
        assert ks.data["iteration"] == 2
        ks.increment_iteration()
        assert ks.data["iteration"] == 3


class TestGetIterationSummaryNotFound:
    """Additional edge case for get_iteration_summary."""

    def test_missing_iteration_returns_none(self, ks):
        ks.add_iteration_summary(1, "Summary for iter 1")
        assert ks.get_iteration_summary(999) is None


class TestSanitizeForJson:
    """Tests for _sanitize_for_json helper."""

    def test_nan_replaced_with_none(self):
        assert _sanitize_for_json(float("nan")) is None

    def test_inf_replaced_with_none(self):
        assert _sanitize_for_json(float("inf")) is None

    def test_neg_inf_replaced_with_none(self):
        assert _sanitize_for_json(float("-inf")) is None

    def test_normal_float_unchanged(self):
        assert _sanitize_for_json(3.14) == 3.14

    def test_zero_unchanged(self):
        assert _sanitize_for_json(0.0) == 0.0

    def test_non_float_types_unchanged(self):
        assert _sanitize_for_json(42) == 42
        assert _sanitize_for_json("hello") == "hello"
        assert _sanitize_for_json(True) is True
        assert _sanitize_for_json(None) is None

    def test_nan_in_list(self):
        result = _sanitize_for_json([1.0, float("nan"), 3.0])
        assert result == [1.0, None, 3.0]

    def test_nan_in_dict(self):
        result = _sanitize_for_json({"a": float("nan"), "b": 2.0})
        assert result == {"a": None, "b": 2.0}

    def test_deeply_nested(self):
        data = {"groups": [{"values": [float("nan"), float("inf"), 1.5]}]}
        result = _sanitize_for_json(data)
        assert result == {"groups": [{"values": [None, None, 1.5]}]}

    def test_none_passthrough(self):
        assert _sanitize_for_json(None) is None

    def test_empty_containers(self):
        assert _sanitize_for_json({}) == {}
        assert _sanitize_for_json([]) == []


class TestGetReportOutline:
    """Tests for get_report_outline() — used by the report-writing agent."""

    def test_includes_full_abstracts(self, ks):
        abstract = "A" * 1000
        ks.add_literature("12345678", "My Paper Title", abstract)
        outline = ks.get_report_outline()
        assert abstract in outline

    def test_includes_literature_titles_and_pmids(self, ks):
        ks.add_literature("99999999", "Some Important Paper", "abstract text")
        outline = ks.get_report_outline()
        assert "Some Important Paper" in outline
        assert "PMID: 99999999" in outline

    def test_omits_abstract_line_when_empty(self, ks):
        ks.add_literature("11111111", "No Abstract Paper", "")
        outline = ks.get_report_outline()
        assert "No Abstract Paper" in outline
        assert "Abstract:" not in outline

    def test_includes_findings_and_hypotheses(self, ks):
        ks.add_finding("Important Discovery", "p < 0.001")
        ks.add_hypothesis("X causes Y")
        outline = ks.get_report_outline()
        assert "Important Discovery" in outline
        assert "X causes Y" in outline

    def test_includes_finding_citations(self, ks):
        ks.add_literature("55555555", "Cited Paper", "relevant abstract text here")
        ks.add_finding(
            "A Finding",
            "p = 0.01",
            citations=[
                {
                    "pmid": "55555555",
                    "snippet": "relevant abstract text here",
                    "explanation": "Supports the finding directly",
                }
            ],
        )
        outline = ks.get_report_outline()
        assert "PMID:55555555" in outline
        assert "relevant abstract text here" in outline
        assert "Supports the finding directly" in outline
        assert "[verified]" in outline

    def test_omits_abstracts_for_cited_papers(self, ks):
        ks.add_literature("77777777", "Cited Paper", "This abstract should be omitted")
        ks.add_literature("88888888", "Uncited Paper", "This abstract should appear")
        ks.add_finding(
            "A Finding",
            "evidence",
            citations=[{"pmid": "77777777", "snippet": "This abstract", "explanation": "test"}],
        )
        outline = ks.get_report_outline()
        # Cited paper's abstract should NOT appear in the literature section
        assert "Abstract: This abstract should be omitted" not in outline
        # Uncited paper's abstract SHOULD appear
        assert "Abstract: This abstract should appear" in outline

    def test_report_outline_evidence_included(self, ks):
        ks.add_finding("My Finding", "p < 0.001, d = 0.8")
        outline = ks.get_report_outline()
        assert "Statistical evidence: p < 0.001, d = 0.8" in outline


class TestToDict:
    """Tests for raw dict access."""

    def test_returns_data_dict(self, ks):
        d = ks.to_dict()
        assert isinstance(d, dict)
        assert "config" in d
        assert "hypotheses" in d

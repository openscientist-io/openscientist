"""Tests for knowledge_state module."""

import pytest

from shandy.knowledge_state import KnowledgeState


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


class TestSaveAndLoad:
    """Tests for file persistence with locking."""

    def test_save_and_load_roundtrip(self, tmp_path):
        ks = KnowledgeState("j1", "Question?", 10)
        ks.add_hypothesis("Hyp 1")
        ks.add_finding("Find 1", "evidence")

        fp = tmp_path / "ks.json"
        ks.save(fp)

        loaded = KnowledgeState.load(fp)
        assert loaded.data["config"]["job_id"] == "j1"
        assert len(loaded.data["hypotheses"]) == 1
        assert len(loaded.data["findings"]) == 1

    def test_save_creates_parent_dirs(self, tmp_path):
        ks = KnowledgeState("j1", "Q?", 5)
        fp = tmp_path / "deep" / "nested" / "ks.json"
        ks.save(fp)
        assert fp.exists()

    def test_save_overwrites_existing(self, tmp_path):
        fp = tmp_path / "ks.json"

        ks1 = KnowledgeState("j1", "Q?", 5)
        ks1.add_hypothesis("Original")
        ks1.save(fp)

        ks2 = KnowledgeState.load(fp)
        ks2.add_hypothesis("New")
        ks2.save(fp)

        loaded = KnowledgeState.load(fp)
        assert len(loaded.data["hypotheses"]) == 2


class TestVersionInfo:
    """Tests for version info metadata."""

    def test_set_version_info(self, ks):
        ks.set_version_info({"claude_model": "claude-4", "shandy_commit": "abc123"})
        assert ks.data["config"]["version_info"]["claude_model"] == "claude-4"


class TestToDict:
    """Tests for raw dict access."""

    def test_returns_data_dict(self, ks):
        d = ks.to_dict()
        assert isinstance(d, dict)
        assert "config" in d
        assert "hypotheses" in d

"""Unit tests for :mod:`openscientist.references.validator`.

Covers citation extraction, validation, auto-correction (including the
preprint-tag and idempotency rules), the References-section builder, the
trailing-generated-section stripper, the NCBI fail-soft path, and the
end-to-end ``validate_report`` flow. The end-to-end test monkeypatches
``fetch_pubmed`` so the suite is hermetic — no NCBI calls.
"""

from __future__ import annotations

import urllib.error

import pytest

from openscientist.references import validator
from openscientist.references.validator import (
    Citation,
    PubMedRecord,
    _build_correction,
    _strip_trailing_generated_section,
    apply_corrections,
    build_references_section,
    extract_citations,
    fetch_pubmed,
    validate,
    validate_report,
)


def _rec(
    pmid: str,
    surnames: list[str],
    *,
    year: str = "2024",
    title: str = "Test title",
    preprint: bool = False,
) -> PubMedRecord:
    """Build a PubMedRecord stub mimicking PubMed's ``Lastname Initials`` form."""
    authors = [f"{s} XY" for s in surnames]
    return PubMedRecord(
        pmid=pmid,
        title=title,
        authors=authors,
        first_author=authors[0] if authors else "",
        year=year,
        source="J Test",
        pubtypes=["Preprint"] if preprint else ["Journal Article"],
    )


# --------------------------------------------------------------------- extract


class TestExtractCitations:
    def test_simple_pair(self) -> None:
        text = "Smith et al. (2020) showed X ([PMID: 12345678](url))."
        cites = extract_citations(text)
        assert len(cites) == 1
        assert cites[0].pmid == "12345678"
        assert cites[0].cited_author == "Smith et al."
        assert cites[0].cited_year == 2020

    def test_single_author_no_et_al(self) -> None:
        cites = extract_citations("As Frieden (2015) demonstrated (PMID: 25377861).")
        assert cites[0].cited_author == "Frieden"
        assert cites[0].cited_year == 2015

    def test_co_first_authors(self) -> None:
        cites = extract_citations("Naguib & Lopez-Lee et al. (2025) found Y (PMID: 40555238).")
        assert cites[0].cited_author == "Naguib & Lopez-Lee et al."

    def test_accented_surname(self) -> None:
        cites = extract_citations("Soto-Faguás et al. (2025) (PMID: 41279783)")
        assert cites[0].cited_author == "Soto-Faguás et al."

    def test_pairing_breaks_at_paragraph(self) -> None:
        text = (
            "Smith et al. (2020) studied X.\n\n(PMID: 12345678) is about something else entirely."
        )
        assert extract_citations(text)[0].cited_author is None

    def test_pairing_beyond_400_chars_fails(self) -> None:
        text = "Smith et al. (2020) " + "x " * 250 + " (PMID: 12345678)"
        assert extract_citations(text)[0].cited_author is None

    def test_pmid_without_attribution(self) -> None:
        cites = extract_citations("See (PMID: 12345678) for details.")
        assert len(cites) == 1
        assert cites[0].cited_author is None


# -------------------------------------------------------------------- validate


def _make_cite(author: str | None, year: int | None) -> Citation:
    return Citation(
        pmid="1",
        cited_author=author,
        cited_year=year,
        pmid_span=(0, 10),
        author_span=(0, 20),
    )


class TestValidate:
    def test_correct_citation_no_issues(self) -> None:
        rec = _rec("1", ["Smith", "Jones"], year="2020")
        assert validate(_make_cite("Smith et al.", 2020), rec) == []

    def test_wrong_author_flagged(self) -> None:
        rec = _rec("1", ["Smith"], year="2020")
        issues = validate(_make_cite("Wrong et al.", 2020), rec)
        assert any("NOT on the author list" in i for i in issues)

    def test_on_paper_but_not_first(self) -> None:
        rec = _rec("1", ["Smith", "Jones"], year="2020")
        issues = validate(_make_cite("Jones et al.", 2020), rec)
        assert any("not as first author" in i for i in issues)

    def test_year_mismatch(self) -> None:
        rec = _rec("1", ["Smith"], year="2020")
        issues = validate(_make_cite("Smith et al.", 2019), rec)
        assert any("2019" in i and "2020" in i for i in issues)

    def test_preprint_flagged(self) -> None:
        rec = _rec("1", ["Smith"], year="2020", preprint=True)
        issues = validate(_make_cite("Smith et al.", 2020), rec)
        assert any("preprint" in i.lower() for i in issues)

    def test_unresolvable_pmid(self) -> None:
        rec = PubMedRecord(pmid="9999")  # empty title => unresolved
        issues = validate(_make_cite("Smith et al.", 2020), rec)
        assert any("did not resolve" in i for i in issues)


# ------------------------------------------------------------ _build_correction


class TestBuildCorrection:
    def test_wrong_author_corrected(self) -> None:
        rec = _rec("1", ["Perez-Corredor", "Marino"], year="2025")
        assert _build_correction("Lopera et al.", 2025, rec) == "Perez-Corredor et al. (2025)"

    def test_wrong_year_corrected(self) -> None:
        rec = _rec("1", ["Smith"], year="2026")
        assert _build_correction("Smith et al.", 2025, rec) == "Smith et al. (2026)"

    def test_wrong_author_and_year(self) -> None:
        rec = _rec("1", ["Perez-Corredor"], year="2025")
        assert _build_correction("Lopera et al.", 2019, rec) == "Perez-Corredor et al. (2025)"

    def test_preprint_label_added(self) -> None:
        rec = _rec("1", ["Smith"], year="2025", preprint=True)
        assert _build_correction("Smith et al.", 2025, rec) == "Smith et al. (2025) [Preprint]"

    def test_preprint_already_labeled_no_change(self) -> None:
        rec = _rec("1", ["Smith"], year="2025", preprint=True)
        # author + year already match, and the caller signals "already labeled"
        assert _build_correction("Smith et al.", 2025, rec, preprint_already_labeled=True) is None

    def test_co_first_structure_preserved(self) -> None:
        # When the leading "Naguib" isn't on the paper, the head is replaced
        # but the "& Lopez-Lee et al." structure is preserved (v1 limitation).
        rec = _rec("1", ["Akay", "Tsai"], year="2025")
        assert (
            _build_correction("Naguib & Lopez-Lee et al.", 2025, rec)
            == "Akay & Lopez-Lee et al. (2025)"
        )

    def test_nothing_wrong_returns_none(self) -> None:
        rec = _rec("1", ["Smith"], year="2020")
        assert _build_correction("Smith et al.", 2020, rec) is None


# ------------------------------------------------------------ apply_corrections


class TestApplyCorrections:
    def _setup(self, text: str, rec: PubMedRecord):
        cites = extract_citations(text)
        cites_with_issues = [(c, validate(c, rec)) for c in cites]
        return cites_with_issues, {rec.pmid: rec}

    def test_wrong_author_rewritten(self) -> None:
        text = "As Lopera et al. (2025) described (PMID: 40637118)."
        rec = _rec("40637118", ["Perez-Corredor"], year="2025")
        cites_with_issues, records = self._setup(text, rec)
        out, corrections = apply_corrections(text, cites_with_issues, records)
        assert "Perez-Corredor et al. (2025)" in out
        assert "Lopera" not in out
        assert len(corrections) == 1

    def test_no_warning_markers(self) -> None:
        text = "As Lopera et al. (2025) described (PMID: 40637118)."
        rec = _rec("40637118", ["Perez-Corredor"], year="2025")
        cites_with_issues, records = self._setup(text, rec)
        out, _ = apply_corrections(text, cites_with_issues, records)
        assert "⚠" not in out

    def test_preprint_inline_label_added(self) -> None:
        text = "As Smith et al. (2025) noted (PMID: 99999999)."
        rec = _rec("99999999", ["Smith"], year="2025", preprint=True)
        cites_with_issues, records = self._setup(text, rec)
        out, corrections = apply_corrections(text, cites_with_issues, records)
        assert "[Preprint]" in out
        assert len(corrections) == 1

    def test_preprint_already_labeled_not_doubled(self) -> None:
        text = "As Smith et al. (2025) [Preprint] noted (PMID: 99999999)."
        rec = _rec("99999999", ["Smith"], year="2025", preprint=True)
        cites_with_issues, records = self._setup(text, rec)
        out, _ = apply_corrections(text, cites_with_issues, records)
        assert out.count("[Preprint]") == 1


# ------------------------------------------------------- build_references_section


def test_references_section_format() -> None:
    records = {
        "1": _rec("1", ["Smith", "Jones"], year="2020"),
        "2": _rec("2", ["Brown"], year="2021", title="Other", preprint=True),
    }
    section = build_references_section(records, ["1", "2"])
    assert "## References" in section
    assert "Smith XY" in section
    assert "[Preprint]" in section
    assert "[1](https://pubmed.ncbi.nlm.nih.gov/1/)" in section


def test_references_section_truncates_at_3_authors() -> None:
    records = {
        "1": _rec("1", ["A", "B", "C", "D", "E", "F"], year="2020"),
    }
    section = build_references_section(records, ["1"])
    assert "A XY, B XY, C XY, et al." in section


# ----------------------------------------------------------- strip-trailing


class TestStripTrailingGeneratedSection:
    def test_strips_references_section(self) -> None:
        text = "Body text.\n\n---\n\n## References\n\n1. Foo (2020).\n"
        out = _strip_trailing_generated_section(text)
        assert "## References" not in out
        assert "Body text." in out

    def test_strips_citation_issues_too(self) -> None:
        text = (
            "Body.\n\n---\n\n## Citation Issues (auto-detected)\n\n- PMID 1\n"
            "\n---\n\n## References\n\n1. Foo.\n"
        )
        out = _strip_trailing_generated_section(text)
        assert "Citation Issues" not in out
        assert "## References" not in out

    def test_no_section_returns_unchanged(self) -> None:
        text = "Just a body, no sections.\n"
        assert _strip_trailing_generated_section(text) == text


# ----------------------------------------------------- fetch_pubmed fail-soft


class TestFetchPubmedFailSoft:
    def test_url_error_returns_empty_stubs(self, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise urllib.error.URLError("NCBI down")

        monkeypatch.setattr(validator.urllib.request, "urlopen", boom)
        out = fetch_pubmed(["12345678", "87654321"])
        assert set(out) == {"12345678", "87654321"}
        assert all(rec.title == "" for rec in out.values())

    def test_malformed_json_returns_empty_stubs(self, monkeypatch) -> None:
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not valid json"

        monkeypatch.setattr(validator.urllib.request, "urlopen", lambda *a, **k: FakeResp())
        out = fetch_pubmed(["12345678"])
        assert out["12345678"].title == ""


# ---------------------------------------------------- validate_report end-to-end


@pytest.fixture
def hermetic_fetch(monkeypatch):
    """Replace ``fetch_pubmed`` with a fixture-driven mock."""

    def _install(records: dict[str, PubMedRecord]):
        def fake_fetch(pmids):
            return {p: records.get(p, PubMedRecord(pmid=p)) for p in pmids}

        monkeypatch.setattr(validator, "fetch_pubmed", fake_fetch)

    return _install


def test_validate_report_corrects_and_appends_references(tmp_path, hermetic_fetch) -> None:
    hermetic_fetch({"40637118": _rec("40637118", ["Perez-Corredor", "Marino"], year="2025")})
    report = tmp_path / "final_report.md"
    report.write_text(
        "# Test Report\n\nAs Lopera et al. (2025) described ([PMID: 40637118](url)).\n"
    )
    output, summary = validate_report(report)
    assert "Perez-Corredor et al. (2025)" in output
    assert "Lopera" not in output
    assert "⚠" not in output
    assert "## References" in output
    assert summary["n_corrected"] == 1
    assert summary["corrections"][0]["pmid"] == "40637118"


def test_validate_report_idempotent_on_resaved_output(tmp_path, hermetic_fetch) -> None:
    hermetic_fetch({"40637118": _rec("40637118", ["Perez-Corredor"], year="2025")})
    report = tmp_path / "report.md"
    report.write_text("As Lopera et al. (2025) noted (PMID: 40637118).\n")

    out1, _ = validate_report(report)
    report.write_text(out1)
    out2, _ = validate_report(report)
    assert out1 == out2
    assert out1.count("## References") == 1


def test_validate_report_strips_agent_written_references(tmp_path, hermetic_fetch) -> None:
    hermetic_fetch({"40637118": _rec("40637118", ["Perez-Corredor"], year="2025")})
    # Agent freehand-wrote its own References section; ours should replace it.
    report = tmp_path / "report.md"
    report.write_text(
        "Body with Perez-Corredor et al. (2025) (PMID: 40637118).\n"
        "\n---\n\n## References\n\n"
        "1. Some hand-written entry that should be replaced.\n"
    )
    output, _ = validate_report(report)
    assert output.count("## References") == 1
    assert "Some hand-written entry" not in output

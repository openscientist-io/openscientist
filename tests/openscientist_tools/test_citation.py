"""Unit tests for the ``validate_citation`` MCP tool.

Includes the **regression test for the JF/MR/KM bug** (commit af1ec53): with
``actual_first_author`` returning just the surname, a model that defaults to
"FirstName Lastname" English convention can no longer grab a trailing token
of initials and produce ``"JF et al."``-style attributions.
"""

from __future__ import annotations

from openscientist.references.validator import PubMedRecord
from openscientist_tools import citation as citation_module
from openscientist_tools.citation import validate_citation


def _patch_fetch(monkeypatch, records: dict[str, PubMedRecord]) -> None:
    monkeypatch.setattr(
        citation_module,
        "fetch_pubmed",
        lambda pmids: {p: records.get(p, PubMedRecord(pmid=p)) for p in pmids},
    )


def _rec(
    pmid: str,
    surnames: list[str],
    *,
    year: str = "2024",
    title: str = "Test",
    preprint: bool = False,
) -> PubMedRecord:
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


# ============================== regression: JF/MR fix ==========================


class TestJFRegression:
    """Pin the surname-only return so the JF/MR/KM failure mode can't return.

    PubMed's ``sortfirstauthor`` is ``"Lastname Initials"`` (e.g.
    ``"Arboleda-Velasquez JF"``). Before af1ec53 the tool returned that raw
    string as ``actual_first_author``, an LLM reading "Use this surname"
    grabbed the trailing token ("JF"), and the report ended up with citations
    like ``"JF et al."``. The fix returns just the surname.
    """

    def test_actual_first_author_is_surname_only(self, monkeypatch) -> None:
        _patch_fetch(
            monkeypatch,
            {"31686034": _rec("31686034", ["Arboleda-Velasquez"], year="2019")},
        )
        res = validate_citation(author="Smith", pmid="31686034", year=2019)
        assert res["actual_first_author"] == "Arboleda-Velasquez"
        # The raw "Lastname Initials" form is exposed only in a clearly-named
        # transparency field — agents must NOT parse this for the surname.
        assert res["pubmed_first_author_raw"] == "Arboleda-Velasquez XY"

    def test_suggested_citation_has_no_initials(self, monkeypatch) -> None:
        _patch_fetch(
            monkeypatch,
            {"31686034": _rec("31686034", ["Arboleda-Velasquez"], year="2019")},
        )
        res = validate_citation(author="Smith", pmid="31686034", year=2019)
        # The bug was producing citations like "JF et al." — the suggested
        # citation must never contain the bare initials token.
        assert "XY" not in res["suggested_citation"]
        assert res["suggested_citation"].startswith("Arboleda-Velasquez")

    def test_issue_message_does_not_embed_raw_initials(self, monkeypatch) -> None:
        """If the agent copies from `issues` instead of `actual_first_author`,
        we don't want it to encounter the raw "Lastname Initials" form there
        either — that's how the bug was re-introducible."""
        _patch_fetch(
            monkeypatch,
            {"31686034": _rec("31686034", ["Arboleda-Velasquez"], year="2019")},
        )
        res = validate_citation(author="Smith", pmid="31686034", year=2019)
        for msg in res["issues"]:
            assert "XY" not in msg, f"issue leaks raw initials: {msg!r}"


# ============================== is_valid semantics ==============================


class TestIsValidSemantics:
    """``is_valid`` must be True iff there are no issues — an agent reading
    ``is_valid=True`` should be able to use the citation as-is without
    consulting ``issues``."""

    def test_clean_citation(self, monkeypatch) -> None:
        _patch_fetch(monkeypatch, {"1": _rec("1", ["Smith"], year="2020")})
        res = validate_citation(author="Smith", pmid="1", year=2020)
        assert res["is_valid"] is True
        assert res["issues"] == []

    def test_on_paper_but_not_first_is_invalid(self, monkeypatch) -> None:
        _patch_fetch(monkeypatch, {"1": _rec("1", ["Smith", "Jones"], year="2020")})
        res = validate_citation(author="Jones", pmid="1", year=2020)
        # On the paper, year matches — but not the first author. Codex flagged
        # the prior behaviour (is_valid=True here) as misleading. Now: invalid.
        assert res["is_valid"] is False
        assert res["on_author_list"] is True
        assert res["is_first_author"] is False
        assert res["issues"], "expected at least one issue"

    def test_preprint_is_invalid_until_labeled(self, monkeypatch) -> None:
        _patch_fetch(monkeypatch, {"1": _rec("1", ["Smith"], year="2020", preprint=True)})
        res = validate_citation(author="Smith", pmid="1", year=2020)
        assert res["is_valid"] is False
        assert res["is_preprint"] is True


# ============================== unresolved + year + preprint ===================


class TestOtherCheks:
    def test_unresolved_pmid(self, monkeypatch) -> None:
        # PubMed returns nothing for this PMID — empty stub record.
        _patch_fetch(monkeypatch, {})
        res = validate_citation(author="Smith", pmid="9999")
        assert res["is_valid"] is False
        assert res["actual_first_author"] == ""
        assert res["on_author_list"] is False
        assert any("did not resolve" in i for i in res["issues"])

    def test_year_mismatch(self, monkeypatch) -> None:
        _patch_fetch(monkeypatch, {"1": _rec("1", ["Smith"], year="2020")})
        res = validate_citation(author="Smith", pmid="1", year=2019)
        assert res["is_valid"] is False
        assert any("2019" in i and "2020" in i for i in res["issues"])

    def test_pmid_with_prefix_accepted(self, monkeypatch) -> None:
        _patch_fetch(monkeypatch, {"123": _rec("123", ["Smith"], year="2020")})
        res = validate_citation(author="Smith", pmid="PMID:123", year=2020)
        assert res["is_valid"] is True
        assert res["actual_first_author"] == "Smith"

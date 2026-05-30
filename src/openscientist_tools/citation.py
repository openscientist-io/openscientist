"""MCP tool: verify a citation's surname/year against PubMed.

The agent calls this **before** writing each ``Surname et al. YEAR (PMID)``
attribution in the final report, so wrong-author citations are caught at
composition time rather than only at the post-process gate. See
:mod:`openscientist.references.validator` for the post-process counterpart
(auto-correction + References section).
"""

from __future__ import annotations

from openscientist.references.validator import _norm, fetch_pubmed
from openscientist_tools.server import mcp


@mcp.tool()
def validate_citation(
    author: str,
    pmid: str,
    year: int | None = None,
) -> dict:
    """Verify an "Author et al. Year (PMID)" attribution against PubMed before writing it.

    Catches the common failure mode where a recognizable name from the domain
    literature gets pinned on a real PMID whose topic you remember — e.g.
    citing "Lopera et al. (PMID: 40637118)" when Lopera isn't among the 15
    actual authors (the real first author is Perez-Corredor). **Call this for
    every "Surname et al. Year" attribution you intend to write in the final
    report**, and use the returned ``suggested_citation`` if anything's flagged.

    Args:
        author: The cited surname (e.g. "Lopera"). Strip "et al.", year, and
            any punctuation — just the leading surname.
        pmid: The PubMed ID, digits only (or with "PMID:" prefix — both work).
        year: Optional. The year you plan to cite. Will be checked against the
            paper's publication year.

    Returns:
        A dict with:

        - ``is_valid`` (bool) — True iff the cited surname is on the paper AND
          the year matches (if given) AND the paper is not a preprint.
        - ``on_author_list`` (bool) — Whether the cited surname appears anywhere
          on the paper's author list.
        - ``is_first_author`` (bool) — Whether the cited surname matches the
          actual first author (the conventional citation handle).
        - ``actual_first_author`` (str) — PubMed's first author (e.g.
          "Perez-Corredor P"). **Use this surname** if ``on_author_list`` is
          False.
        - ``actual_year`` (str) — Publication year per PubMed.
        - ``is_preprint`` (bool) — True for bioRxiv/medRxiv/etc. Label preprints
          as "[Preprint]".
        - ``suggested_citation`` (str) — A correctly-formed attribution string
          you can use directly if the validator flagged an issue.
        - ``issues`` (list[str]) — Human-readable issues. Empty list = clean.
    """
    pmid_clean = str(pmid).strip().lstrip("PMID:").strip()
    records = fetch_pubmed([pmid_clean])
    rec = records.get(pmid_clean)
    if not rec or not rec.title:
        return {
            "is_valid": False,
            "on_author_list": False,
            "is_first_author": False,
            "actual_first_author": "",
            "actual_year": "",
            "is_preprint": False,
            "suggested_citation": "",
            "issues": [f"PMID {pmid_clean} did not resolve at PubMed"],
        }

    head = (
        author.strip().split(" et al")[0].split(" &")[0].split(" and ")[0].strip()
    )
    author_norm = _norm(head)
    author_norms = [_norm(s) for s in rec.surnames]
    first_surname = rec.surnames[0] if rec.surnames else ""
    first_norm = _norm(first_surname)

    on_list = author_norm in author_norms
    is_first = bool(first_norm) and author_norm == first_norm
    year_match = year is None or (bool(rec.year) and str(year) == rec.year)
    is_preprint = rec.is_preprint

    issues: list[str] = []
    if not on_list:
        issues.append(
            f"'{head}' is NOT on the author list (actual first author: "
            f"{rec.first_author})"
        )
    elif not is_first:
        issues.append(
            f"'{head}' is on the paper but is not the first author "
            f"(actual first author: {rec.first_author})"
        )
    if year is not None and not year_match:
        issues.append(f"cited year {year} but PubMed says {rec.year}")
    if is_preprint:
        issues.append("paper is a preprint — label as [Preprint]")

    has_others = len(rec.surnames) > 1
    suggested = (
        f"{first_surname} et al. ({rec.year})"
        if has_others
        else f"{first_surname} ({rec.year})"
    )
    if is_preprint:
        suggested += " [Preprint]"

    return {
        "is_valid": on_list and year_match and not is_preprint,
        "on_author_list": on_list,
        "is_first_author": is_first,
        "actual_first_author": rec.first_author,
        "actual_year": rec.year,
        "is_preprint": is_preprint,
        "suggested_citation": suggested,
        "issues": issues,
    }

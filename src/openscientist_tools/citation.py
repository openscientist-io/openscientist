"""MCP tool: verify a citation's surname/year against PubMed.

Catches wrong-author citations at composition time, before the post-process
gate in :mod:`openscientist.references.validator` has to rewrite them.
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
    """Verify an "Author et al. Year (PMID)" attribution against PubMed.

    Catches the failure mode where a recognizable name from the field gets
    pinned on a real PMID whose topic the agent remembers (e.g. "Lopera et al.
    (PMID: 40637118)" when Lopera isn't on that paper). Use ``suggested_citation``
    from the response if anything is flagged.

    Args:
        author: The cited surname; strip ``et al.``, year, and punctuation.
        pmid: PubMed ID (digits, with or without ``PMID:`` prefix).
        year: Optional year to check against the paper's publication year.

    Returns a dict:

        is_valid (bool): True iff ``issues`` is empty.
        on_author_list, is_first_author (bool): granular checks.
        actual_first_author (str): surname only (e.g. ``"Perez-Corredor"``).
            PubMed returns ``"Lastname Initials"``; this field strips the
            initials so you don't have to parse the format.
        actual_year (str), is_preprint (bool), suggested_citation (str).
        pubmed_first_author_raw (str): the raw ``"Lastname Initials"`` string,
            for transparency only — do NOT use it as the citation surname.
        issues (list[str]): human-readable issues; empty = clean.
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

    head = author.strip().split(" et al")[0].split(" &")[0].split(" and ")[0].strip()
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
        issues.append(f"'{head}' is NOT on the author list — use surname '{first_surname}'")
    elif not is_first:
        issues.append(
            f"'{head}' is on the paper but is not the first author — use surname '{first_surname}'"
        )
    if year is not None and not year_match:
        issues.append(f"cited year {year} but PubMed says {rec.year}")
    if is_preprint:
        issues.append("paper is a preprint — label as [Preprint]")

    has_others = len(rec.surnames) > 1
    suggested = (
        f"{first_surname} et al. ({rec.year})" if has_others else f"{first_surname} ({rec.year})"
    )
    if is_preprint:
        suggested += " [Preprint]"

    return {
        # is_valid is True iff every check passed — so an agent that sees
        # is_valid=True can write the citation as-is without consulting issues.
        "is_valid": not issues,
        "on_author_list": on_list,
        "is_first_author": is_first,
        "actual_first_author": first_surname,
        "actual_year": rec.year,
        "is_preprint": is_preprint,
        "suggested_citation": suggested,
        "issues": issues,
        # The raw PubMed "Lastname Initials" string for debugging/transparency.
        # Do NOT parse this for the citation surname — use actual_first_author.
        "pubmed_first_author_raw": rec.first_author,
    }

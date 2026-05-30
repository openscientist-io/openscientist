"""Validate inline citations + generate a References section for a report.

Two checks (deterministic, no LLM):

1. **Author surname is on the paper.** For every ``Surname et al. YEAR (PMID)``
   citation in the markdown, ``Surname`` must appear on the PubMed author list
   for that PMID. Three severity tiers:

   - cited surname is the actual first author (or PubMed's ``sortfirstauthor``)
     → ✓ clean
   - cited surname is on the paper but not first author
     → ⚠ "on paper but not first author"
   - cited surname is not on the paper at all (the hallucination case)
     → ✗ "not on author list"

2. **Year matches PubMed.** Mismatched years are flagged.

Plus a third pass that builds a deduplicated **References section**, with
first-3-authors-then-et-al display, the journal, year, and PMID hyperlink,
flagged ``[Preprint]`` where applicable.

The output is the original report with ⚠ markers adjacent to each problem PMID
and a generated References section appended — i.e. soft-annotation (no rewrite
of the prose, no opinion).

Run::

    python3 -m openscientist.references.validator path/to/final_report.md

Writes ``<input>.annotated.md`` and ``<input>.refs.json``. Stdlib only — no
dependency on the OS env.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

# PMID mention. 7–9 digits (covers all PMIDs assigned to date).
PMID_RE = re.compile(r"PMID[\s:]+(\d{7,9})")

# "Author Year" attribution patterns observed in Marina's report:
#   Surname et al. (2019)              — standard
#   Surname (2015)                     — single author
#   Surname & Surname et al. (2025)    — declared co-first authors
#   Surname-Surname et al. (2025)      — hyphenated surnames
#   Soto-Faguás                        — accented characters
# Surname allows uppercase initial, lowercase letters, accented Latin chars,
# hyphens, and apostrophes (e.g. O'Hare).
_SURNAME = r"[A-Z][\w'À-ſ-]+"
AUTHOR_YEAR_RE = re.compile(
    rf"({_SURNAME}(?:\s+(?:&|and)\s+{_SURNAME})?(?:\s+et\s+al\.?)?)\s*\((\d{{4}})\)"
)


@dataclass
class Citation:
    """One PMID mention in the source, paired with the nearest preceding attribution."""

    pmid: str
    cited_author: str | None  # raw "Lopera et al." (or None if no nearby attribution)
    cited_year: int | None
    pmid_span: tuple[int, int]  # char offsets of the PMID match in source
    author_span: tuple[int, int] | None


@dataclass
class PubMedRecord:
    pmid: str
    title: str = ""
    authors: list[str] = field(default_factory=list)  # raw "Surname Initials"
    first_author: str = ""  # ``sortfirstauthor`` from esummary
    year: str = ""
    source: str = ""  # journal abbreviation
    pubtypes: list[str] = field(default_factory=list)

    @property
    def is_preprint(self) -> bool:
        return any("preprint" in t.lower() for t in self.pubtypes)

    @property
    def surnames(self) -> list[str]:
        # PubMed author strings are "Surname Initials" — surname is up to the last space.
        return [a.rsplit(" ", 1)[0] for a in self.authors if a]


def extract_citations(text: str) -> list[Citation]:
    """Find every PMID; pair with the closest preceding (Author, Year) within 400
    chars and the same paragraph."""
    pmid_matches = list(PMID_RE.finditer(text))
    ay_matches = list(AUTHOR_YEAR_RE.finditer(text))
    cites: list[Citation] = []
    for pm in pmid_matches:
        chosen = None
        for m in reversed(ay_matches):
            if m.end() > pm.start():
                continue
            if pm.start() - m.end() > 400:
                break
            if "\n\n" in text[m.end() : pm.start()]:
                continue
            chosen = m
            break
        cites.append(
            Citation(
                pmid=pm.group(1),
                cited_author=chosen.group(1) if chosen else None,
                cited_year=int(chosen.group(2)) if chosen else None,
                pmid_span=(pm.start(), pm.end()),
                author_span=(chosen.start(), chosen.end()) if chosen else None,
            )
        )
    return cites


def fetch_pubmed(pmids: list[str]) -> dict[str, PubMedRecord]:
    """Batched esummary fetch (NCBI guidance: ≤200 IDs/req, 3 req/sec)."""
    out: dict[str, PubMedRecord] = {}
    batch_size = 100
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i : i + batch_size]
        params = urllib.parse.urlencode(
            {"db": "pubmed", "id": ",".join(batch), "retmode": "json"}
        )
        url = f"{EUTILS}?{params}"
        resp = json.load(urllib.request.urlopen(url, timeout=20))["result"]
        for pmid in batch:
            rec = resp.get(pmid, {})
            # Filter out non-author contributors (CollectiveName, etc.) if marked.
            authors = [
                a.get("name", "")
                for a in (rec.get("authors") or [])
                if a.get("authtype") in (None, "Author") and a.get("name")
            ]
            pubdate = rec.get("pubdate", "")
            year = pubdate[:4] if pubdate[:4].isdigit() else ""
            out[pmid] = PubMedRecord(
                pmid=pmid,
                title=rec.get("title", ""),
                authors=authors,
                first_author=rec.get("sortfirstauthor", "")
                or (authors[0] if authors else ""),
                year=year,
                source=rec.get("source", ""),
                pubtypes=rec.get("pubtype") or [],
            )
        if i + batch_size < len(pmids):
            time.sleep(0.4)  # be polite if multiple batches
    return out


def _norm(s: str) -> str:
    """Strip diacritics + lowercase; "Soto-Faguás" -> "soto-faguas"."""
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def validate(c: Citation, rec: PubMedRecord) -> list[str]:
    """Return a list of issue strings for one citation. Empty list = clean."""
    issues: list[str] = []
    if not rec.title:
        issues.append(f"PMID {c.pmid} did not resolve at PubMed")
        return issues

    if c.cited_author is not None:
        # The leading surname from "Lopera et al." / "Naguib & Lopez-Lee et al." etc.
        head = c.cited_author.split(" et al")[0].split(" &")[0].split(" and ")[0].strip()
        cited_norm = _norm(head)
        author_norms = [_norm(s) for s in rec.surnames]
        first_norm = _norm(rec.surnames[0]) if rec.surnames else ""

        if cited_norm == first_norm:
            pass  # ✓ cited as first author
        elif cited_norm in author_norms:
            issues.append(
                f"cited '{c.cited_author}' is on the paper but not as first author "
                f"(actual first author: {rec.first_author})"
            )
        else:
            issues.append(
                f"cited '{c.cited_author}' but '{head}' is NOT on the author list "
                f"(actual first author: {rec.first_author})"
            )

    if c.cited_year is not None and rec.year and str(c.cited_year) != rec.year:
        issues.append(f"cited year {c.cited_year} but PubMed says {rec.year}")

    if rec.is_preprint:
        issues.append("preprint — should be labeled as such")

    return issues


def format_reference(rec: PubMedRecord, n: int) -> str:
    """One numbered References-section line, Vancouver-ish style."""
    if not rec.title:
        return f"{n}. PMID: [{rec.pmid}](https://pubmed.ncbi.nlm.nih.gov/{rec.pmid}/) — *could not resolve*"
    authors_disp = (
        ", ".join(rec.authors[:3]) + ", et al."
        if len(rec.authors) > 3
        else ", ".join(rec.authors)
    )
    preprint = " *[Preprint]*" if rec.is_preprint else ""
    return (
        f"{n}. {authors_disp}. {rec.title} *{rec.source}*. {rec.year}.{preprint} "
        f"PMID: [{rec.pmid}](https://pubmed.ncbi.nlm.nih.gov/{rec.pmid}/)"
    )


def _build_correction(
    cited_author: str, cited_year: int | None, rec: PubMedRecord
) -> str | None:
    """Build a corrected ``Surname[ et al.] (YEAR)`` string for an attribution
    whose surname isn't on the paper or whose year is wrong, else ``None``.

    Replaces only the leading surname with PubMed's actual first author when
    the cited name isn't on the paper, and replaces the year when it doesn't
    match. Preserves the original "et al." / co-first / single-author structure
    — "Naguib & Lopez-Lee et al." becomes "Akay & Lopez-Lee et al." rather than
    collapsing the structure (which might over-correct if the second name is
    legitimately on the paper).
    """
    if not rec.surnames:
        return None
    head = cited_author.split(" et al")[0].split(" &")[0].split(" and ")[0].strip()
    cited_norm = _norm(head)
    author_norms = [_norm(s) for s in rec.surnames]

    author_wrong = cited_norm not in author_norms
    year_wrong = (
        cited_year is not None and rec.year and str(cited_year) != rec.year
    )
    if not (author_wrong or year_wrong):
        return None

    correct_first = rec.surnames[0]
    new_author = (
        cited_author.replace(head, correct_first, 1) if author_wrong else cited_author
    )
    new_year = (
        rec.year if year_wrong else (str(cited_year) if cited_year else rec.year)
    )
    return f"{new_author} ({new_year})"


def correct_and_annotate(
    text: str,
    cites_with_issues: list[tuple[Citation, list[str]]],
    records: dict[str, PubMedRecord],
) -> tuple[str, list[dict]]:
    """Auto-correct wrong-author / wrong-year attributions in the prose and
    insert ⚠ markers after each flagged PMID.

    Returns ``(annotated_text, corrections)`` where ``corrections`` is a list
    of ``{pmid, original, corrected}`` dicts for the sidecar JSON. All edits
    are applied from end to start so character offsets stay valid through the
    rewrite, and author-span corrections never overlap PMID-span markers.
    """
    edits: list[tuple[int, int, str]] = []
    corrections: list[dict] = []

    for cite, issues in cites_with_issues:
        if not issues:
            continue
        # ⚠ marker right after the PMID match (zero-width insertion).
        edits.append((cite.pmid_span[1], cite.pmid_span[1], " ⚠"))
        # Author/year correction in the attribution span.
        rec = records.get(cite.pmid)
        if cite.author_span and cite.cited_author and rec and rec.title:
            replacement = _build_correction(cite.cited_author, cite.cited_year, rec)
            if replacement is not None:
                original = text[cite.author_span[0] : cite.author_span[1]]
                edits.append((cite.author_span[0], cite.author_span[1], replacement))
                corrections.append(
                    {
                        "pmid": cite.pmid,
                        "original": original,
                        "corrected": replacement,
                    }
                )

    for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
        text = text[:start] + replacement + text[end:]
    return text, corrections


def build_citation_issues_section(
    cites_with_issues: list[tuple[Citation, list[str]]],
    corrections: list[dict],
) -> str:
    """Build the human-readable "Citation Issues (auto-detected)" panel listing
    each unique flagged PMID, its issues, and the auto-correction if any.
    Empty string when nothing was flagged."""
    flagged = [(c, iss) for c, iss in cites_with_issues if iss]
    if not flagged:
        return ""
    seen: set[str] = set()
    unique_flags: list[tuple[Citation, list[str]]] = []
    for c, iss in flagged:
        if c.pmid in seen:
            continue
        seen.add(c.pmid)
        unique_flags.append((c, iss))
    corr_by_pmid = {c["pmid"]: c for c in corrections}

    lines = ["", "---", "", "## Citation Issues (auto-detected)", ""]
    lines.append(
        "Citations flagged during post-processing. Wrong-author and wrong-year "
        "attributions have been auto-corrected in the prose (⚠ marker next to "
        "each affected PMID); preprints are tagged in the References section "
        "but left in the body for the author to handle."
    )
    lines.append("")
    for c, issues in unique_flags:
        corr = corr_by_pmid.get(c.pmid)
        joined = "; ".join(issues)
        if corr:
            lines.append(
                f'- **PMID {c.pmid}** — auto-corrected *"{corr["original"]}"* → '
                f'*"{corr["corrected"]}"*. {joined}'
            )
        else:
            lines.append(f"- **PMID {c.pmid}** — {joined}")
    return "\n".join(lines) + "\n"


def build_references_section(
    records: dict[str, PubMedRecord], pmids_in_order: list[str]
) -> str:
    lines = ["", "---", "", "## References", ""]
    for i, pmid in enumerate(pmids_in_order, 1):
        lines.append(format_reference(records[pmid], i))
    return "\n".join(lines) + "\n"


def validate_report(report_path: Path) -> tuple[str, dict]:
    """Validate citations and build a References section for a markdown report.

    Returns ``(annotated_md, summary)``. ``annotated_md`` is the original report
    text with a ⚠ marker after each problematic PMID and a generated References
    section appended. ``summary`` is the structured validation result (counts,
    per-flag details, and the resolved reference list) — JSON-serializable.

    This is the library entry point. The CLI (:func:`main`) writes its outputs
    to ``<input>.annotated.md`` / ``<input>.refs.json``; the orchestrator hook
    (:func:`annotate_in_place`) overwrites ``final_report.md`` in place so the
    downstream HTML/PDF render picks up the annotations.
    """
    text = report_path.read_text()
    cites = extract_citations(text)
    unique_pmids = sorted({c.pmid for c in cites})
    records = fetch_pubmed(unique_pmids)
    cites_with_issues = [(c, validate(c, records[c.pmid])) for c in cites]
    flagged = [(c, iss) for c, iss in cites_with_issues if iss]
    flagged_pmids = {c.pmid for c, _ in flagged}

    annotated_body, corrections = correct_and_annotate(
        text, cites_with_issues, records
    )
    issues_section = build_citation_issues_section(cites_with_issues, corrections)
    refs_section = build_references_section(records, unique_pmids)
    annotated = annotated_body.rstrip() + "\n" + issues_section + refs_section

    summary = {
        "n_citation_instances": len(cites),
        "n_unique_pmids": len(unique_pmids),
        "n_flagged_instances": len(flagged),
        "n_flagged_pmids": len(flagged_pmids),
        "n_corrected": len(corrections),
        "corrections": corrections,
        "flagged": [
            {
                "pmid": c.pmid,
                "cited_author": c.cited_author,
                "cited_year": c.cited_year,
                "issues": iss,
            }
            for c, iss in flagged
        ],
        "references": [
            {
                "pmid": r.pmid,
                "first_author": r.first_author,
                "title": r.title,
                "year": r.year,
                "source": r.source,
                "preprint": r.is_preprint,
                "n_authors": len(r.authors),
            }
            for r in (records[p] for p in unique_pmids)
        ],
    }
    return annotated, summary


def annotate_in_place(report_path: Path) -> dict:
    """Run :func:`validate_report` and write the results back next to the report.

    Overwrites ``report_path`` with the annotated markdown so the HTML/PDF
    pipeline picks up ⚠ markers + the References section automatically. Writes
    a JSON sidecar at ``<report_dir>/final_report.refs.json``. Returns the
    summary dict so the caller can log/expose it.
    """
    annotated, summary = validate_report(report_path)
    report_path.write_text(annotated, encoding="utf-8")
    sidecar = report_path.parent / "final_report.refs.json"
    sidecar.write_text(
        json.dumps({"report": str(report_path), **summary}, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", type=Path, help="path to final_report.md")
    ap.add_argument(
        "--out-md", type=Path, default=None, help="(default: <input>.annotated.md)"
    )
    ap.add_argument(
        "--out-json", type=Path, default=None, help="(default: <input>.refs.json)"
    )
    args = ap.parse_args()

    annotated, summary = validate_report(args.report)
    print(
        f"extracted {summary['n_citation_instances']} citation instances, "
        f"{summary['n_unique_pmids']} unique PMIDs",
        file=sys.stderr,
    )
    print(
        f"\n{summary['n_flagged_instances']}/{summary['n_citation_instances']} "
        f"citation instances flagged ({summary['n_flagged_pmids']} distinct PMIDs)\n",
        file=sys.stderr,
    )
    for f in summary["flagged"]:
        print(f"  PMID {f['pmid']}: {'; '.join(f['issues'])}", file=sys.stderr)

    out_md = args.out_md or args.report.with_suffix(".annotated.md")
    out_json = args.out_json or args.report.with_suffix(".refs.json")
    out_md.write_text(annotated)
    out_json.write_text(json.dumps({"report": str(args.report), **summary}, indent=2))
    print(f"\nwrote {out_md}", file=sys.stderr)
    print(f"wrote {out_json}", file=sys.stderr)


if __name__ == "__main__":
    main()

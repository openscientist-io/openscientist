"""Validate inline citations and fix them deterministically.

For every ``Surname et al. YEAR (PMID)`` attribution in the report, this pass:

* checks ``Surname`` appears on the PubMed author list for that PMID — and
  rewrites the prose with PubMed's actual first author when it doesn't;
* checks the cited year against PubMed's publication year — and rewrites it
  when it doesn't match;
* tags preprints (bioRxiv / medRxiv / etc.) with ``[Preprint]`` inline so
  the reader sees publication status at the point of citation;
* appends a deduplicated **References section** at the end of the report
  with first-3-authors-then-et-al display, journal, year, ``[Preprint]``
  tag where applicable, and a PMID hyperlink.

Every rewrite is recorded in a JSON sidecar (``final_report.refs.json``)
for audit — the prose itself stays clean (no inline warning markers, no
"Citation Issues" panel cluttering the report).

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
    cited_author: str,
    cited_year: int | None,
    rec: PubMedRecord,
    *,
    preprint_already_labeled: bool = False,
) -> str | None:
    """Build a corrected ``Surname[ et al.] (YEAR)[ [Preprint]]`` string for an
    attribution that's wrong in some way, else ``None``.

    Three things this fixes, in order of how much the prose changes:

    * **Wrong year** — replaces the year with PubMed's publication year.
    * **Wrong author** — replaces only the leading surname with PubMed's actual
      first author. The original "et al." / co-first / single-author structure
      is preserved ("Naguib & Lopez-Lee et al." becomes "Akay & Lopez-Lee
      et al." rather than collapsing — the second-name verification is a v2
      improvement; over-collapsing here might silently drop legitimate
      co-author info).
    * **Unlabeled preprint** — appends ``[Preprint]`` to the attribution so
      the publication status is visible inline. Skipped if the prose already
      labels it (``preprint_already_labeled``), which the caller detects from
      the text immediately after the author span.

    Returns ``None`` when nothing needs to change.
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
    preprint_to_label = rec.is_preprint and not preprint_already_labeled
    if not (author_wrong or year_wrong or preprint_to_label):
        return None

    correct_first = rec.surnames[0]
    new_author = (
        cited_author.replace(head, correct_first, 1) if author_wrong else cited_author
    )
    new_year = (
        rec.year if year_wrong else (str(cited_year) if cited_year else rec.year)
    )
    base = f"{new_author} ({new_year})"
    if preprint_to_label:
        base += " [Preprint]"
    return base


def apply_corrections(
    text: str,
    cites_with_issues: list[tuple[Citation, list[str]]],
    records: dict[str, PubMedRecord],
) -> tuple[str, list[dict]]:
    """Rewrite wrong-author / wrong-year / unlabeled-preprint attributions in
    the prose, in place. No warning markers — bad citations become correct
    citations, full stop. The audit trail lives in the returned ``corrections``
    list (and from there in the JSON sidecar).

    Returns ``(corrected_text, corrections)`` where ``corrections`` is a list
    of ``{pmid, original, corrected}`` dicts. All edits are applied from end to
    start so character offsets stay valid through the rewrite.
    """
    edits: list[tuple[int, int, str]] = []
    corrections: list[dict] = []

    for cite, issues in cites_with_issues:
        if not issues:
            continue
        rec = records.get(cite.pmid)
        if not (cite.author_span and cite.cited_author and rec and rec.title):
            continue
        # Look ~30 chars past the attribution to detect an already-present
        # preprint tag — keeps the rewrite idempotent for this cite.
        suffix = text[cite.author_span[1] : cite.author_span[1] + 30]
        preprint_labeled = (
            "[Preprint]" in suffix or "preprint" in suffix.lower()
        )
        replacement = _build_correction(
            cite.cited_author,
            cite.cited_year,
            rec,
            preprint_already_labeled=preprint_labeled,
        )
        if replacement is None:
            continue
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


def build_references_section(
    records: dict[str, PubMedRecord], pmids_in_order: list[str]
) -> str:
    lines = ["", "---", "", "## References", ""]
    for i, pmid in enumerate(pmids_in_order, 1):
        lines.append(format_reference(records[pmid], i))
    return "\n".join(lines) + "\n"


# Matches a trailing "## References" or "## Citation Issues" heading (with an
# optional preceding `---` separator). Anchored to the LAST such heading in the
# document so we can strip everything from there to EOF before appending a
# fresh auto-generated section. Idempotent for re-runs and replaces an
# agent-freehand References section with the verified one.
_TRAILING_GENERATED_HEADING_RE = re.compile(
    r"\n+(?:---[ \t]*\n+)?##[ \t]+(?:References|Citation Issues\b[^\n]*)\n"
)


def _strip_trailing_generated_section(text: str) -> str:
    """Strip an agent-written or previously-auto-generated trailing References
    / Citation Issues section so the freshly built one can be appended without
    doubling. Stripping is anchored to the EARLIEST matching heading so a stale
    "Citation Issues" panel + "References" pair from an older run both go."""
    matches = list(_TRAILING_GENERATED_HEADING_RE.finditer(text))
    if not matches:
        return text
    return text[: matches[0].start()].rstrip() + "\n"


def validate_report(report_path: Path) -> tuple[str, dict]:
    """Validate citations, fix them in place, and append a References section.

    Returns ``(output_md, summary)``. ``output_md`` is the report with bad
    citations rewritten and a deduplicated References section at the end — no
    warning markers in the prose. ``summary`` is the structured audit trail
    (counts, per-correction original/corrected pairs, resolved reference list)
    for the JSON sidecar.

    This is the library entry point. The CLI (:func:`main`) writes its outputs
    to ``<input>.annotated.md`` / ``<input>.refs.json``; the orchestrator hook
    (:func:`annotate_in_place`) overwrites ``final_report.md`` in place so the
    downstream HTML/PDF render picks up the rewrites.
    """
    text = report_path.read_text()
    cites = extract_citations(text)
    unique_pmids = sorted({c.pmid for c in cites})
    records = fetch_pubmed(unique_pmids)
    cites_with_issues = [(c, validate(c, records[c.pmid])) for c in cites]
    flagged = [(c, iss) for c, iss in cites_with_issues if iss]
    flagged_pmids = {c.pmid for c, _ in flagged}

    corrected_body, corrections = apply_corrections(text, cites_with_issues, records)
    corrected_body = _strip_trailing_generated_section(corrected_body)
    refs_section = build_references_section(records, unique_pmids)
    annotated = corrected_body.rstrip() + "\n" + refs_section

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

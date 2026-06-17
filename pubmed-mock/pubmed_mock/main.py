"""Minimal NCBI eutils mock for air-gap mode validation.

Exposes ``/esearch.fcgi`` and ``/efetch.fcgi`` over HTTP, served from a
small pre-bundled corpus (~400 abstracts targeted at the smoke-test
research areas: torpor, hibernation, hypothermia, POA/KOR signaling,
brain metabolomics). It is **not** a real PubMed mirror; agents that
search for topics outside the bundled corpus will see empty result sets,
which the agent then surfaces as "no literature found" — the correct
behavior in air-gap mode with a limited mirror.

The API contract matches what ``openscientist.literature.search_pubmed``
needs:

* ``GET /esearch.fcgi?term=...&retmax=N&retmode=json`` →
  ``{"esearchresult": {"idlist": ["PMID", ...]}}``
* ``GET /efetch.fcgi?db=pubmed&id=PMID1,PMID2&retmode=xml`` →
  the relevant ``<PubmedArticle>`` blocks concatenated inside a
  ``<PubmedArticleSet>`` envelope.

Search is a case-insensitive substring match against each abstract's
title + abstract text (built once at startup from the bundled XML).
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"

app = FastAPI(
    title="OpenScientist Airgap PubMed Mock",
    description="NCBI eutils mock for air-gap validation. Not a real mirror.",
)


def _load_corpus() -> tuple[dict[str, ET.Element], dict[str, str]]:
    """Return (pmid → PubmedArticle Element, pmid → searchable_text)."""
    xml_path = CORPUS_DIR / "abstracts.xml"
    if not xml_path.exists():
        logger.warning("No corpus at %s — server will return empty results", xml_path)
        return {}, {}

    raw = xml_path.read_text()
    # The fetch script wrote one <PubmedArticleSet> per batch; concatenate
    # by extracting every <PubmedArticle>...</PubmedArticle> block.
    articles_by_pmid: dict[str, ET.Element] = {}
    searchable_by_pmid: dict[str, str] = {}

    block_pattern = re.compile(r"<PubmedArticle\b.*?</PubmedArticle>", re.DOTALL)
    for block in block_pattern.findall(raw):
        try:
            elem = ET.fromstring(block)
        except ET.ParseError as exc:
            logger.warning("Skipping unparseable article block: %s", exc)
            continue
        pmid_el = elem.find(".//PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        pmid = pmid_el.text.strip()
        articles_by_pmid[pmid] = elem

        # Build a searchable text blob: title + abstract text + keywords.
        text_parts: list[str] = []
        title_el = elem.find(".//ArticleTitle")
        if title_el is not None and title_el.text:
            text_parts.append(title_el.text)
        for ab in elem.findall(".//AbstractText"):
            if ab.text:
                text_parts.append(ab.text)
        for kw in elem.findall(".//Keyword"):
            if kw.text:
                text_parts.append(kw.text)
        searchable_by_pmid[pmid] = " ".join(text_parts).lower()

    return articles_by_pmid, searchable_by_pmid


ARTICLES, SEARCHABLE = _load_corpus()
logger.info("Loaded %d articles from corpus", len(ARTICLES))


@app.get("/")
def root() -> dict[str, Any]:
    manifest_path = CORPUS_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    return {
        "service": "openscientist-airgap-pubmed-mock",
        "articles_loaded": len(ARTICLES),
        "corpus_manifest": manifest,
    }


@app.get("/health")
def health() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/esearch.fcgi")
def esearch(
    db: str = "pubmed",
    term: str = "",
    retmax: int = 20,
    retmode: str = "json",
    email: str | None = None,
) -> Response:
    """Return PMIDs whose title/abstract matches every whitespace-split
    token of ``term`` (case-insensitive).

    The real eutils API supports complex query syntax (``AND``, ``OR``,
    field-qualified terms like ``[Title]``). We treat the term as a plain
    AND of whitespace-split words — sufficient for the agent's typical
    ``"hypothermia brain metabolomics"``-style queries.
    """
    del db, email  # not used in mock
    tokens = [t.lower() for t in term.split() if t and t.lower() not in {"and", "or"}]
    if not tokens:
        idlist: list[str] = []
    else:
        idlist = [
            pmid
            for pmid, text in SEARCHABLE.items()
            if all(tok in text for tok in tokens)
        ]
    idlist = idlist[: max(0, int(retmax))]

    if retmode == "json":
        return JSONResponse(
            {
                "esearchresult": {
                    "count": str(len(idlist)),
                    "retmax": str(len(idlist)),
                    "retstart": "0",
                    "idlist": idlist,
                    "translationset": [],
                    "querytranslation": term,
                }
            }
        )
    # Fallback: minimal XML response for non-JSON callers.
    items = "".join(f"<Id>{p}</Id>" for p in idlist)
    return Response(
        content=(
            f"<?xml version='1.0'?><eSearchResult><Count>{len(idlist)}</Count>"
            f"<IdList>{items}</IdList></eSearchResult>"
        ),
        media_type="application/xml",
    )


@app.get("/efetch.fcgi")
def efetch(
    db: str = "pubmed",
    id: str = Query(default=""),  # noqa: A002 — eutils param name
    retmode: str = "xml",
    email: str | None = None,
) -> Response:
    """Return ``<PubmedArticle>`` records for the requested PMIDs wrapped
    in a ``<PubmedArticleSet>`` envelope, matching the real eutils
    response shape that ``literature._parse_pubmed_xml`` expects.
    """
    del db, retmode, email  # mock always returns XML
    pmids = [p.strip() for p in id.split(",") if p.strip()]
    if not pmids:
        return Response(
            content="<?xml version='1.0'?><PubmedArticleSet/>",
            media_type="application/xml",
        )

    blocks = []
    for pmid in pmids:
        article = ARTICLES.get(pmid)
        if article is None:
            continue
        blocks.append(ET.tostring(article, encoding="unicode"))

    body = (
        "<?xml version='1.0'?><PubmedArticleSet>"
        + "".join(blocks)
        + "</PubmedArticleSet>"
    )
    return Response(content=body, media_type="application/xml")

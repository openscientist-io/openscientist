"""
Literature search via PubMed API for OpenScientist.
"""

import time
from typing import Any

import requests

from openscientist.settings import get_settings


def search_pubmed(
    query: str, max_results: int = 10, email: str | None = None
) -> list[dict[str, Any]]:
    """Search PubMed and return relevant papers.

    With the airgap setting enabled, the local mirror is searched instead.
    An email is optional but recommended for NCBI.
    """
    if get_settings().airgap.enabled:
        from openscientist.pubmed_mirror.query import search_local_sync

        return search_local_sync(query, max_results=max_results)

    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    search_params: dict[str, str | int] = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
    }
    if email:
        search_params["email"] = email

    search_response = requests.get(f"{base_url}/esearch.fcgi", params=search_params, timeout=10)
    search_response.raise_for_status()

    search_data = search_response.json()
    pmids = search_data.get("esearchresult", {}).get("idlist", [])

    if not pmids:
        return []

    time.sleep(0.34)  # Max 3 requests per second

    fetch_params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
    if email:
        fetch_params["email"] = email

    fetch_response = requests.get(f"{base_url}/efetch.fcgi", params=fetch_params, timeout=10)
    fetch_response.raise_for_status()

    return _parse_pubmed_xml(fetch_response.text, pmids)


def _parse_pubmed_xml(xml_text: str, pmids: list[str]) -> list[dict[str, Any]]:
    """Parse PubMed XML response."""
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text)

        papers = []
        for article in root.findall(".//PubmedArticle"):
            try:
                pmid_elem = article.find(".//PMID")
                pmid = pmid_elem.text if pmid_elem is not None else "Unknown"

                title_elem = article.find(".//ArticleTitle")
                title = (
                    ("".join(title_elem.itertext()).strip() or "No title")
                    if title_elem is not None
                    else "No title"
                )

                abstract_elems = article.findall(".//AbstractText")
                abstract_parts = [
                    t for t in ("".join(e.itertext()).strip() for e in abstract_elems) if t
                ]
                abstract = " ".join(abstract_parts) if abstract_parts else "No abstract available"

                author_elems = article.findall(".//Author/LastName")
                authors = ", ".join([elem.text for elem in author_elems if elem.text])
                if not authors:
                    authors = "Unknown authors"

                year_elem = article.find(".//PubDate/Year")
                year = year_elem.text if year_elem is not None else "Unknown year"

                papers.append(
                    {
                        "pmid": pmid,
                        "title": title,
                        "abstract": abstract,
                        "authors": authors,
                        "year": year,
                    }
                )
            except (AttributeError, KeyError, TypeError):
                # Skip malformed articles
                continue

        return papers

    except Exception as e:
        return [
            {
                "pmid": pmid,
                "title": "Error parsing paper",
                "abstract": f"Could not parse XML: {e!s}",
                "authors": "",
                "year": "",
            }
            for pmid in pmids
        ]

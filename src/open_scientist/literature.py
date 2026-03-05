"""
Literature search via PubMed API for OpenScientist.

Proactive literature integration to inform hypothesis generation.
"""

import time
from typing import Any

import requests


def search_pubmed(
    query: str, max_results: int = 10, email: str | None = None
) -> list[dict[str, Any]]:
    """
    Search PubMed and return relevant papers.

    Args:
        query: Search terms (e.g., "hypothermia neuroprotection metabolomics")
        max_results: Number of papers to return (default: 10)
        email: Email for NCBI (optional but recommended)

    Returns:
        List of papers with abstracts:
        [
            {
                "pmid": "12345678",
                "title": "Paper title",
                "abstract": "Abstract text",
                "authors": "Author1, Author2",
                "year": "2023"
            },
            ...
        ]
    """
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    # Step 1: Search for PMIDs
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

    # Step 2: Fetch paper details
    # Be nice to NCBI servers - rate limit
    time.sleep(0.34)  # Max 3 requests per second

    fetch_params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
    if email:
        fetch_params["email"] = email

    fetch_response = requests.get(f"{base_url}/efetch.fcgi", params=fetch_params, timeout=10)
    fetch_response.raise_for_status()

    # Parse XML response (simplified - in production use proper XML parser)
    return _parse_pubmed_xml(fetch_response.text, pmids)


def _parse_pubmed_xml(xml_text: str, pmids: list[str]) -> list[dict[str, Any]]:
    """
    Parse PubMed XML response.

    This is a simplified parser. For production, use xml.etree.ElementTree or lxml.

    Args:
        xml_text: XML response from PubMed
        pmids: List of PMIDs to extract

    Returns:
        List of paper dictionaries
    """
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text)

        papers = []
        for article in root.findall(".//PubmedArticle"):
            try:
                # Extract PMID
                pmid_elem = article.find(".//PMID")
                pmid = pmid_elem.text if pmid_elem is not None else "Unknown"

                # Extract title
                title_elem = article.find(".//ArticleTitle")
                title = title_elem.text if title_elem is not None else "No title"

                # Extract abstract
                abstract_elems = article.findall(".//AbstractText")
                abstract_parts = [elem.text for elem in abstract_elems if elem.text]
                abstract = " ".join(abstract_parts) if abstract_parts else "No abstract available"

                # Extract authors
                author_elems = article.findall(".//Author/LastName")
                authors = ", ".join([elem.text for elem in author_elems if elem.text])
                if not authors:
                    authors = "Unknown authors"

                # Extract year
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
        # Fallback: return minimal info
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


def format_literature_for_prompt(papers: list[dict[str, Any]], max_papers: int = 5) -> str:
    """
    Format literature references for inclusion in prompts.

    Args:
        papers: List of papers
        max_papers: Maximum number to include

    Returns:
        Formatted string
    """
    if not papers:
        return "No literature references yet."

    lines = ["## Relevant Literature\n"]
    lines.extend(
        f"- **PMID {paper['pmid']}** ({paper.get('year', 'N/A')}): {paper['title'][:100]}..."
        for paper in papers[:max_papers]
    )

    return "\n".join(lines)

"""Standalone `search_pubmed` tool."""

from __future__ import annotations

import asyncio
from typing import Any

from openscientist.knowledge_state import KnowledgeState
from openscientist.literature import search_pubmed as _backend_search_pubmed
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE


@mcp.tool()
async def search_pubmed(query: str, max_results: int = 10, description: str = "") -> str:
    """Search PubMed for scientific papers.

    Args:
        query: Search query (e.g., 'hypothermia neuroprotection metabolomics')
        max_results: Maximum number of results to return (default: 10)
        description: Why you're searching

    Returns:
        Formatted list of papers with titles, abstracts, and PMIDs
    """
    ks = await KnowledgeState.load_from_database(STATE.job_id)

    short_query = query[:60] + "..." if len(query) > 60 else query
    ks.set_agent_status(f"Searching PubMed: {short_query}")
    await ks.save_to_database(STATE.job_id)

    papers = await asyncio.to_thread(_backend_search_pubmed, query, max_results=max_results)

    for paper in papers:
        ks.add_literature(
            pmid=paper["pmid"],
            title=paper["title"],
            abstract=paper["abstract"],
            search_query=query,
        )

    ks.log_analysis(
        action="search_pubmed",
        query=query,
        results_count=len(papers),
        description=description,
    )
    await ks.save_to_database(STATE.job_id)

    if not papers:
        return f"No papers found for query: '{query}'"

    return _format_papers_markdown(query, papers)


def _format_papers_markdown(query: str, papers: list[dict[str, Any]]) -> str:
    parts = [f"Found {len(papers)} papers for query: '{query}'\n"]
    for i, paper in enumerate(papers, 1):
        parts.append(
            f"\n{i}. **{paper['title']}** (PMID: {paper['pmid']}, {paper.get('year', 'N/A')})\n"
            f"   Authors: {paper.get('authors', 'Unknown')}\n"
            f"   Abstract: {paper['abstract'][:1500]}\n"
        )
    return "".join(parts)

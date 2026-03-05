"""
PubMed search tool for the SDK agent path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from openscientist.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)


def make_tools(ctx: ToolContext) -> list[Callable[..., Any]]:
    """Return the search_pubmed tool bound to ctx."""

    @tool
    def search_pubmed(query: str, max_results: int = 10, description: str = "") -> str:
        """
        Search PubMed for scientific papers.

        Args:
            query: Search query (e.g., 'hypothermia neuroprotection metabolomics')
            max_results: Maximum number of results to return (default: 10)
            description: Why you're searching

        Returns:
            Formatted list of papers with titles, abstracts, and PMIDs
        """
        from openscientist.knowledge_state import KnowledgeState
        from openscientist.literature import search_pubmed as search_pm

        ks = KnowledgeState.load(ctx.ks_path)

        short_query = query[:60] + "..." if len(query) > 60 else query
        ks.set_agent_status(f"Searching PubMed: {short_query}")
        ks.save(ctx.ks_path)

        papers = search_pm(query, max_results=max_results)

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
        ks.save(ctx.ks_path)

        if not papers:
            return f"No papers found for query: '{query}'"

        parts = [f"Found {len(papers)} papers for query: '{query}'\n"]
        for i, paper in enumerate(papers, 1):
            parts.append(
                f"\n{i}. **{paper['title']}** (PMID: {paper['pmid']}, {paper.get('year', 'N/A')})\n"
                f"   Authors: {paper.get('authors', 'Unknown')}\n"
                f"   Abstract: {paper['abstract'][:300]}...\n"
            )
        return "".join(parts)

    return [search_pubmed]

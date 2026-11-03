"""Query the local PubMed corpus via Postgres full-text search.

Returns the same dict shape as ``openscientist.literature.search_pubmed`` so
the reroute is transparent to callers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.session import async_session_local

# websearch_to_tsquery is injection-safe and never raises on arbitrary input.
_SEARCH_SQL = text(
    """
    SELECT pmid, title, abstract, authors, year
    FROM pubmed_articles
    WHERE search_vector @@ websearch_to_tsquery('english', :query)
    ORDER BY ts_rank_cd(search_vector, websearch_to_tsquery('english', :query)) DESC
    LIMIT :limit
    """
)


def _row_to_paper(row: Any) -> dict[str, Any]:
    return {
        "pmid": str(row.pmid),
        "title": row.title,
        "abstract": row.abstract or "No abstract available",
        "authors": row.authors or "Unknown authors",
        "year": str(row.year) if row.year is not None else "Unknown year",
    }


async def run_search(
    session: AsyncSession, query: str, max_results: int = 10
) -> list[dict[str, Any]]:
    if not query.strip() or max_results <= 0:
        return []
    result = await session.execute(_SEARCH_SQL, {"query": query, "limit": max_results})
    return [_row_to_paper(row) for row in result.all()]


async def search_local(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    async with async_session_local() as session:
        return await run_search(session, query, max_results)


def search_local_sync(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    from openscientist.async_tasks import run_sync

    return run_sync(search_local(query, max_results))

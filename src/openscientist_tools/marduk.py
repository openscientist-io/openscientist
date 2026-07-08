"""MARDUK tools: Monarch Initiative knowledge-graph access.

Registered only when ``STATE.marduk_enabled`` (per-job MARDUK mode). Monarch
tools are thin wrappers over ``openscientist.monarch``. Every tool does the
standard ``KnowledgeState`` provenance bookkeeping, mirroring
``openscientist_tools.pubmed``.
"""

from __future__ import annotations

from openscientist import monarch
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE


def search_monarch(query: str, category: str = "", limit: int = 10) -> str:
    """Search the Monarch Initiative knowledge graph for entities.

    Resolve disease/gene/phenotype names to Monarch CURIEs. Almost every MARDUK
    workflow starts here, because the association/entity tools take CURIEs.

    Args:
        query: Free-text query (e.g. 'Marfan syndrome', 'seizure', 'FBN1').
        category: Optional biolink category filter, e.g. 'biolink:Disease',
            'biolink:Gene', 'biolink:PhenotypicFeature'. Empty = no filter.
        limit: Maximum number of results (default 10).

    Returns:
        Markdown list of matching entities with their CURIEs.
    """
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    short = query[:60] + "..." if len(query) > 60 else query
    ks.set_agent_status(f"Searching Monarch: {short}")
    ks.save_to_database_sync(STATE.job_id)

    entities = monarch.search_entities(query, category=category or None, limit=limit)
    ks.log_analysis(
        action="search_monarch",
        query=query,
        category=category,
        results_count=len(entities),
    )
    ks.save_to_database_sync(STATE.job_id)
    return monarch.format_entities_markdown(query, entities)


def monarch_associations(entity_id: str, category: str = "", limit: int = 20) -> str:
    """Retrieve Monarch knowledge-graph associations for an entity CURIE.

    E.g. disease->phenotype, gene->disease, gene->phenotype. Pass a CURIE
    obtained from ``search_monarch`` (never guess one).

    Args:
        entity_id: Entity CURIE (e.g. 'MONDO:0007947', 'HGNC:3603').
        category: Optional biolink association category, e.g.
            'biolink:DiseaseToPhenotypicFeatureAssociation'. Empty = all.
        limit: Maximum number of associations (default 20).

    Returns:
        Markdown list of associations (subject -predicate-> object).
    """
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    ks.set_agent_status(f"Monarch associations: {entity_id}")
    ks.save_to_database_sync(STATE.job_id)

    assocs = monarch.get_associations(entity_id, category=category or None, limit=limit)
    ks.log_analysis(
        action="monarch_associations",
        entity_id=entity_id,
        category=category,
        results_count=len(assocs),
    )
    ks.save_to_database_sync(STATE.job_id)
    return monarch.format_associations_markdown(entity_id, assocs)


def monarch_entity(entity_id: str) -> str:
    """Fetch the full Monarch record for a CURIE (labels, synonyms, xrefs).

    Args:
        entity_id: Entity CURIE (e.g. 'MONDO:0007947'). Use its xrefs to bridge
            to OMIM/Orphanet identifiers.

    Returns:
        Markdown record for the entity.
    """
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    ks.set_agent_status(f"Monarch entity: {entity_id}")
    ks.save_to_database_sync(STATE.job_id)

    entity = monarch.get_entity(entity_id)
    ks.log_analysis(action="monarch_entity", entity_id=entity_id)
    ks.save_to_database_sync(STATE.job_id)
    return monarch.format_entity_markdown(entity)


if STATE.marduk_enabled:
    mcp.tool()(search_monarch)
    mcp.tool()(monarch_associations)
    mcp.tool()(monarch_entity)

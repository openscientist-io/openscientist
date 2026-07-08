"""MARDUK tools: Monarch + DisMech access + persistent rare-disease memory.

Registered only when ``STATE.marduk_enabled`` (per-job MARDUK mode). Monarch and
DisMech tools are thin wrappers over ``openscientist.monarch`` /
``openscientist.dismech``; memory tools wrap ``openscientist.marduk_memory``.
Every tool does the standard ``KnowledgeState`` provenance bookkeeping, mirroring
``openscientist_tools.pubmed``.
"""

from __future__ import annotations

from uuid import UUID

from openscientist import dismech, marduk_memory, monarch
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE


def _split_tags(tags: str) -> list[str]:
    """Parse a comma-separated tag string into a clean list."""
    return [t.strip() for t in tags.split(",") if t.strip()]


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


def remember_finding(
    title: str,
    insight: str,
    entity_id: str = "",
    entity_label: str = "",
    evidence: str = "",
    tags: str = "",
    kind: str = "insight",
) -> str:
    """Save a durable rare-disease insight to your persistent memory.

    Memories are private to you (the current user) and are recalled by your
    future MARDUK jobs. Record general, reusable conclusions — a confirmed
    disease-gene link, a discriminating phenotype pattern, a ruled-out
    hypothesis, or a useful CURIE mapping — not per-iteration bookkeeping.

    Args:
        title: Short headline for the memory.
        insight: The insight itself (the reusable conclusion).
        entity_id: Primary Monarch CURIE this is about (e.g. 'MONDO:0007947').
        entity_label: Human-readable label for entity_id.
        evidence: Supporting evidence / provenance.
        tags: Comma-separated tags (diseases, genes, phenotypes) for recall.
        kind: Memory type (insight/mapping/ruled_out/association).

    Returns:
        Confirmation string.
    """
    memory_id = marduk_memory.save_memory_sync(
        job_id=STATE.job_id,
        title=title,
        content=insight,
        kind=kind,
        entity_id=entity_id or None,
        entity_label=entity_label or None,
        evidence=evidence or None,
        tags=_split_tags(tags),
    )
    if memory_id is None:
        return "❌ Could not save memory (job owner not found)."

    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    ks.log_analysis(action="remember_finding", title=title, entity_id=entity_id)
    ks.save_to_database_sync(STATE.job_id)
    return f"🧠 Saved memory '{title}' for future jobs."


def recall_memory(query: str = "", entity_id: str = "", limit: int = 10) -> str:
    """Search your persistent memory from previous MARDUK jobs.

    Args:
        query: Free-text search over titles/insights/entities. Empty = most
            recent memories.
        entity_id: Restrict to a specific Monarch CURIE.
        limit: Maximum number of memories to return (default 10).

    Returns:
        Markdown briefing of matching prior memories.
    """
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    owner_id = _job_owner_id()
    if owner_id is None:
        return "No prior MARDUK memories (job owner not found)."

    memories = marduk_memory.recall_memories_sync(
        owner_id=owner_id,
        query=query or None,
        entity_id=entity_id or None,
        exclude_job_id=STATE.job_id,
        limit=limit,
    )
    ks.log_analysis(action="recall_memory", query=query, results_count=len(memories))
    ks.save_to_database_sync(STATE.job_id)
    return marduk_memory.format_memories_markdown(memories)


def list_dismech_disorders(filter: str = "") -> str:
    """List disorders curated in the DisMech disease-mechanisms knowledge base.

    DisMech provides literature-cited pathophysiology, prevalence/epidemiology,
    genetics, and treatments per disorder. Use this to discover which disorders
    are covered, then fetch one with ``get_dismech_disorder``.

    Args:
        filter: Optional case-insensitive substring to narrow the list
            (e.g. 'dysplasia'). Empty = list all.

    Returns:
        Markdown list of disorder names.
    """
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    ks.set_agent_status("Listing DisMech disorders")
    ks.save_to_database_sync(STATE.job_id)

    filenames = dismech.find_disorders(filter) if filter else dismech.list_disorders()
    ks.log_analysis(action="list_dismech_disorders", filter=filter, results_count=len(filenames))
    ks.save_to_database_sync(STATE.job_id)
    return dismech.format_disorder_list_markdown(filter, filenames)


def get_dismech_disorder(name: str, sections: str = "") -> str:
    """Fetch a DisMech disorder's curated mechanism record.

    Returns pathophysiology, prevalence/epidemiology, inheritance, genetics,
    phenotypes, and treatments, with literature-cited evidence. Records are large,
    so use ``sections`` to focus on what you need.

    Args:
        name: Disorder name (e.g. 'Achondroplasia', 'Alagille syndrome'). Matched
            case-insensitively; run ``list_dismech_disorders`` if unsure.
        sections: Optional comma-separated sections to include, from:
            prevalence, inheritance, genetic, pathophysiology,
            mechanistic_hypotheses, phenotypes, treatments, animal_models.
            Empty = all sections.

    Returns:
        Markdown record, or a not-found message with close matches.
    """
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    ks.set_agent_status(f"DisMech: {name}")
    ks.save_to_database_sync(STATE.job_id)

    data = dismech.get_disorder(name)
    if data is None:
        matches = dismech.find_disorders(name)
        ks.log_analysis(action="get_dismech_disorder", name=name, found=False)
        ks.save_to_database_sync(STATE.job_id)
        if matches:
            return dismech.format_disorder_list_markdown(name, matches)
        return f"No DisMech disorder found for '{name}'. Try `list_dismech_disorders`."

    section_list = [s.strip() for s in sections.split(",") if s.strip()] or None
    ks.log_analysis(action="get_dismech_disorder", name=name, found=True, sections=sections)
    ks.save_to_database_sync(STATE.job_id)
    return dismech.format_disorder_markdown(data, sections=section_list)


def _job_owner_id() -> UUID | None:
    """Resolve the current job's owner id for memory scoping."""
    from sqlalchemy import select

    from openscientist.async_tasks import run_sync
    from openscientist.database.models import Job
    from openscientist.database.session import AsyncSessionLocal

    async def _lookup() -> UUID | None:
        async with AsyncSessionLocal(thread_safe=True) as session:
            result = await session.execute(select(Job.owner_id).where(Job.id == UUID(STATE.job_id)))
            return result.scalar_one_or_none()

    return run_sync(_lookup())


if STATE.marduk_enabled:
    mcp.tool()(search_monarch)
    mcp.tool()(monarch_associations)
    mcp.tool()(monarch_entity)
    mcp.tool()(list_dismech_disorders)
    mcp.tool()(get_dismech_disorder)
    mcp.tool()(remember_finding)
    mcp.tool()(recall_memory)

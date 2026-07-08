"""
Monarch Initiative knowledge-graph access for OpenScientist (MARDUK).

Thin, resilient HTTP client over the Monarch REST API v3
(https://api-v3.monarchinitiative.org/v3/api). Mirrors the style of
``literature.py``: ``requests`` with a timeout, ``raise_for_status``, and a
courtesy rate-limit. Response parsing is defensive — the API evolves, so every
field access uses ``.get`` with a fallback rather than assuming a shape.

Used by the ``openscientist_tools.marduk`` MCP tools; kept as a separate backend
module so the tool layer stays a thin bookkeeping wrapper (same split as
``literature.py`` / ``openscientist_tools.pubmed``).
"""

from __future__ import annotations

import time
from typing import Any

import requests

API_BASE = "https://api-v3.monarchinitiative.org/v3/api"

# Courtesy pause between calls so a tight agent loop does not hammer the public
# endpoint (mirrors the PubMed client's rate-limit gesture).
_RATE_LIMIT_SECONDS = 0.2
_DEFAULT_TIMEOUT = 20


class MonarchError(RuntimeError):
    """Raised when a Monarch API request fails after reaching the server."""


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET ``{API_BASE}/{path}`` and return the decoded JSON object."""
    url = f"{API_BASE}/{path.lstrip('/')}"
    # Drop None values so callers can pass optional params uniformly.
    clean = {k: v for k, v in params.items() if v is not None}
    response = requests.get(url, params=clean, timeout=_DEFAULT_TIMEOUT)
    response.raise_for_status()
    time.sleep(_RATE_LIMIT_SECONDS)
    data = response.json()
    if not isinstance(data, dict):
        raise MonarchError(f"Unexpected Monarch response for {url}: not a JSON object")
    return data


def _entity_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a KG entity/search item to a small, stable dict."""
    category = item.get("category")
    if isinstance(category, list):
        category = ", ".join(str(c) for c in category)
    return {
        "id": item.get("id") or item.get("iri") or "UNKNOWN",
        "name": item.get("name") or item.get("label") or "",
        "category": category or "",
        "description": item.get("description") or "",
        "in_taxon_label": item.get("in_taxon_label") or "",
    }


def search_entities(
    query: str,
    *,
    category: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Full-text search the Monarch KG for entities.

    Args:
        query: Free-text query (e.g. "Marfan syndrome", "seizure", "FBN1").
        category: Optional biolink category filter, e.g. "biolink:Disease",
            "biolink:Gene", "biolink:PhenotypicFeature".
        limit: Maximum number of results.

    Returns:
        A list of normalized entity dicts: ``id``, ``name``, ``category``,
        ``description``, ``in_taxon_label``.
    """
    data = _get("search", {"q": query, "category": category, "limit": limit})
    items = data.get("items") or data.get("results") or []
    return [_entity_summary(item) for item in items if isinstance(item, dict)]


def get_entity(entity_id: str) -> dict[str, Any]:
    """Fetch the full node record for a CURIE (e.g. ``MONDO:0007947``).

    Returns a normalized summary plus ``synonyms`` and ``xrefs`` when present.
    Raises :class:`MonarchError` if the entity is missing an id in the response.
    """
    data = _get(f"entity/{entity_id}", {})
    # The v3 entity endpoint returns the node object directly.
    summary = _entity_summary(data)
    if summary["id"] == "UNKNOWN":
        summary["id"] = entity_id
    synonyms = data.get("synonym") or data.get("synonyms") or []
    if isinstance(synonyms, str):
        synonyms = [synonyms]
    summary["synonyms"] = [str(s) for s in synonyms][:20]
    summary["xrefs"] = [str(x) for x in (data.get("xref") or data.get("xrefs") or [])][:40]
    return summary


def get_associations(
    entity_id: str,
    *,
    category: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Retrieve associations for an entity CURIE.

    Args:
        entity_id: Subject/object CURIE (e.g. "MONDO:0007947", "HGNC:3603").
        category: Optional biolink association category, e.g.
            "biolink:DiseaseToPhenotypicFeatureAssociation",
            "biolink:GeneToDiseaseAssociation".
        limit: Maximum number of associations.

    Returns:
        A list of dicts describing each association: ``subject``,
        ``subject_label``, ``predicate``, ``object``, ``object_label``,
        ``category``, ``negated``.
    """
    params: dict[str, Any] = {"entity": entity_id, "category": category, "limit": limit}
    data = _get("association", params)
    items = data.get("items") or data.get("associations") or []
    return [_association_summary(item) for item in items if isinstance(item, dict)]


def _association_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a KG association record to a small, stable dict."""
    category = item.get("category")
    if isinstance(category, list):
        category = ", ".join(str(c) for c in category)
    return {
        "subject": item.get("subject") or "",
        "subject_label": item.get("subject_label") or item.get("subject_name") or "",
        "predicate": item.get("predicate") or "",
        "object": item.get("object") or "",
        "object_label": item.get("object_label") or item.get("object_name") or "",
        "category": category or "",
        "negated": bool(item.get("negated", False)),
    }


def format_entities_markdown(query: str, entities: list[dict[str, Any]]) -> str:
    """Render entity search results as a markdown list for the agent."""
    if not entities:
        return f"No Monarch entities found for query: '{query}'"
    parts = [f"Found {len(entities)} Monarch entities for '{query}':\n"]
    for i, e in enumerate(entities, 1):
        line = f"\n{i}. **{e['name']}** (`{e['id']}`)"
        if e["category"]:
            line += f" — {e['category']}"
        if e["in_taxon_label"]:
            line += f" [{e['in_taxon_label']}]"
        if e["description"]:
            line += f"\n   {e['description'][:400]}"
        parts.append(line + "\n")
    return "".join(parts)


def format_associations_markdown(entity_id: str, assocs: list[dict[str, Any]]) -> str:
    """Render associations as a markdown list for the agent."""
    if not assocs:
        return f"No Monarch associations found for `{entity_id}`"
    parts = [f"Found {len(assocs)} Monarch associations for `{entity_id}`:\n"]
    for i, a in enumerate(assocs, 1):
        subj = f"{a['subject_label']} (`{a['subject']}`)" if a["subject"] else "?"
        obj = f"{a['object_label']} (`{a['object']}`)" if a["object"] else "?"
        predicate = a["predicate"] or a["category"] or "related_to"
        negation = "NOT " if a["negated"] else ""
        parts.append(f"\n{i}. {subj} —{negation}{predicate}→ {obj}\n")
    return "".join(parts)


def format_entity_markdown(entity: dict[str, Any]) -> str:
    """Render a single entity record as markdown for the agent."""
    parts = [f"**{entity['name']}** (`{entity['id']}`)"]
    if entity.get("category"):
        parts.append(f"\nCategory: {entity['category']}")
    if entity.get("in_taxon_label"):
        parts.append(f"\nTaxon: {entity['in_taxon_label']}")
    if entity.get("description"):
        parts.append(f"\n\n{entity['description']}")
    if entity.get("synonyms"):
        parts.append("\n\nSynonyms: " + ", ".join(entity["synonyms"]))
    if entity.get("xrefs"):
        parts.append("\n\nCross-references: " + ", ".join(entity["xrefs"]))
    return "".join(parts)

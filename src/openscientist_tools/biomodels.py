"""Standalone BioModels tools: `search_biomodels` and `fetch_biomodel`.

Thin REST client over the BioModels repository (EBI). No simulator is
imported here: the agent container stays slim and simulation happens in
the executor, where libRoadRunner/COPASI/Antimony are installed. A fetched
model is written under ``provenance/biomodels/`` so it is reachable inside
``execute_code`` at ``/output/biomodels/<id>.xml`` and ships with the job's
artifacts.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import requests

from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE

logger = logging.getLogger(__name__)

BIOMODELS_BASE_URL = "https://www.ebi.ac.uk/biomodels"
_REQUEST_TIMEOUT = 30
_MAX_RESULTS = 50
_MODEL_ID_RE = re.compile(r"^(BIOMD|MODEL)\d{10}$")
# Cheap counts from the SBML text; libsbml is deliberately not a dependency here.
_SBML_SPECIES_RE = re.compile(r"<species\b")
_SBML_REACTION_RE = re.compile(r"<reaction\b")
_SBML_PARAMETER_RE = re.compile(r"<parameter\b")
_SBML_LEVEL_RE = re.compile(r'<sbml\b[^>]*\blevel="(\d+)"[^>]*\bversion="(\d+)"')


def _biomodels_dir() -> Path:
    return STATE.job_dir / "provenance" / "biomodels"


def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = requests.get(
        url, params=params, headers={"Accept": "application/json"}, timeout=_REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected BioModels response shape from {url}")
    return payload


def search_biomodels_backend(query: str, max_results: int) -> list[dict[str, Any]]:
    """Search BioModels; returns a list of ``{id, name, format, submitter, url}``."""
    payload = _get_json(
        f"{BIOMODELS_BASE_URL}/search",
        params={"query": query, "numResults": max_results, "format": "json"},
    )
    # The API treats numResults as a hint and can return more; cap it here.
    models = (payload.get("models") or [])[:max_results]
    results: list[dict[str, Any]] = []
    for m in models:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        results.append(
            {
                "id": str(m["id"]),
                "name": str(m.get("name") or ""),
                "format": str(m.get("format") or ""),
                "submitter": str(m.get("submitter") or ""),
                "url": f"{BIOMODELS_BASE_URL}/{m['id']}",
            }
        )
    return results


def fetch_biomodel_backend(model_id: str, dest_dir: Path) -> dict[str, Any]:
    """Download ``model_id``'s main SBML file into ``dest_dir``.

    Returns metadata (name, publication, format, path) plus cheap structural
    counts parsed from the SBML text.
    """
    meta = _get_json(f"{BIOMODELS_BASE_URL}/{model_id}", params={"format": "json"})
    files = meta.get("files") or {}
    main_files = files.get("main") or []
    if not main_files or not isinstance(main_files[0], dict) or not main_files[0].get("name"):
        raise ValueError(f"BioModels entry {model_id} lists no main model file")
    main_name = str(main_files[0]["name"])

    resp = requests.get(
        f"{BIOMODELS_BASE_URL}/model/download/{model_id}",
        params={"filename": main_name},
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    text = resp.content.decode("utf-8", errors="replace")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{model_id}.xml"
    dest.write_bytes(resp.content)

    pub = meta.get("publication") or {}
    level = _SBML_LEVEL_RE.search(text)
    return {
        "id": model_id,
        "name": str(meta.get("name") or ""),
        "format": str((meta.get("format") or {}).get("name") or main_files[0].get("format") or ""),
        "sbml_level": f"L{level.group(1)}V{level.group(2)}" if level else "",
        "publication_title": str(pub.get("title") or ""),
        "publication_journal": str(pub.get("journal") or ""),
        "publication_year": str(pub.get("year") or ""),
        "publication_link": str(pub.get("link") or ""),
        "file_name": main_name,
        "path": dest,
        "bytes": len(resp.content),
        "n_species": len(_SBML_SPECIES_RE.findall(text)),
        "n_reactions": len(_SBML_REACTION_RE.findall(text)),
        "n_parameters": len(_SBML_PARAMETER_RE.findall(text)),
    }


@mcp.tool()
def search_biomodels(query: str, max_results: int = 10, description: str = "") -> str:
    """Search the BioModels repository for curated systems-biology (SBML) models.

    Use this when a research question concerns the dynamics of a biological
    process (kinetics, signalling, aggregation, pharmacology) and a published
    mechanistic model might exist. Follow up with `fetch_biomodel` to download
    one, then simulate it in `execute_code` with `roadrunner` or `basico`.

    Args:
        query: Keywords, e.g. 'amyloid aggregation', 'MAPK cascade', 'glucose insulin'
        max_results: Maximum number of models to return (default 10, max 50)
        description: Why you're searching

    Returns:
        Formatted list of matching models with IDs, names, and links
    """
    max_results = max(1, min(int(max_results), _MAX_RESULTS))
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    short_query = query[:60] + "..." if len(query) > 60 else query
    ks.set_agent_status(f"Searching BioModels: {short_query}")
    ks.save_to_database_sync(STATE.job_id)

    try:
        models = search_biomodels_backend(query, max_results)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("BioModels search failed for %r: %s", query, exc)
        ks.log_analysis(
            action="search_biomodels",
            query=query,
            success=False,
            error=str(exc),
            description=description,
        )
        ks.save_to_database_sync(STATE.job_id)
        return f"❌ ERROR: BioModels search failed: {exc}"

    ks.log_analysis(
        action="search_biomodels",
        query=query,
        results_count=len(models),
        model_ids=[m["id"] for m in models],
        description=description,
    )
    ks.save_to_database_sync(STATE.job_id)

    if not models:
        return f"No BioModels entries found for query: '{query}'"

    parts = [f"Found {len(models)} BioModels entries for query: '{query}'\n"]
    for i, m in enumerate(models, 1):
        fmt = f", {m['format']}" if m["format"] else ""
        parts.append(f"\n{i}. **{m['name']}** ({m['id']}{fmt})\n   {m['url']}\n")
    parts.append("\nCall `fetch_biomodel(model_id=...)` to download one for simulation.")
    return "".join(parts)


@mcp.tool()
def fetch_biomodel(model_id: str, description: str = "") -> str:
    """Download a BioModels entry (SBML) into this job for simulation.

    The file is saved to `provenance/biomodels/<model_id>.xml` and is
    available inside `execute_code` as `/output/biomodels/<model_id>.xml`:

        import roadrunner
        rr = roadrunner.RoadRunner("/output/biomodels/BIOMD0000000462.xml")
        print(rr.model.getFloatingSpeciesIds()); print(rr.model.getGlobalParameterIds())
        res = rr.simulate(0, 100, 500)

    Args:
        model_id: BioModels identifier, e.g. 'BIOMD0000000462' or 'MODEL1234567890'
        description: Why you're fetching this model

    Returns:
        Model metadata, source publication, structural summary, and the path
    """
    model_id = model_id.strip().upper()
    if not _MODEL_ID_RE.match(model_id):
        return (
            f"❌ ERROR: '{model_id}' is not a BioModels ID (expected BIOMD########## "
            "or MODEL##########). Use `search_biomodels` to find one."
        )

    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    ks.set_agent_status(f"Fetching BioModels entry {model_id}")
    ks.save_to_database_sync(STATE.job_id)

    try:
        info = fetch_biomodel_backend(model_id, _biomodels_dir())
    except (requests.RequestException, ValueError, OSError) as exc:
        logger.warning("BioModels fetch failed for %s: %s", model_id, exc)
        ks.log_analysis(
            action="fetch_biomodel",
            model_id=model_id,
            success=False,
            error=str(exc),
            description=description,
        )
        ks.save_to_database_sync(STATE.job_id)
        return f"❌ ERROR: could not fetch {model_id} from BioModels: {exc}"

    rel_path = Path("provenance") / "biomodels" / info["path"].name
    ks.log_analysis(
        action="fetch_biomodel",
        model_id=model_id,
        model_name=info["name"],
        file=str(rel_path),
        publication=info["publication_link"],
        n_species=info["n_species"],
        n_reactions=info["n_reactions"],
        n_parameters=info["n_parameters"],
        success=True,
        description=description,
    )
    ks.save_to_database_sync(STATE.job_id)

    pub_bits = [
        b
        for b in (
            info["publication_title"],
            info["publication_journal"],
            info["publication_year"],
            info["publication_link"],
        )
        if b
    ]
    fmt = " ".join(b for b in (info["format"], info["sbml_level"]) if b)
    return (
        f"Fetched **{info['name']}** ({model_id}{', ' + fmt if fmt else ''})\n"
        f"- Saved to: `{rel_path}` ({info['bytes']:,} bytes)\n"
        f"- Inside `execute_code`: `/output/biomodels/{model_id}.xml`\n"
        f"- Species: {info['n_species']}, reactions: {info['n_reactions']}, "
        f"parameters: {info['n_parameters']}\n"
        f"- Source: {' | '.join(pub_bits) if pub_bits else 'n/a'}\n"
        f"- BioModels page: {BIOMODELS_BASE_URL}/{model_id}\n\n"
        "Next: load it with `roadrunner.RoadRunner(path)`, list species and parameters, "
        "and time ONE simulation before any parameter scan."
    )

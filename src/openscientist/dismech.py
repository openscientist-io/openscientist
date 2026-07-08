"""
DisMech (Disorder Mechanisms Knowledge Base) access for OpenScientist (MARDUK).

DisMech (https://github.com/monarch-initiative/dismech) is a curated knowledge
base of disease *pathophysiology* — mechanism narratives, phenotypes (HPO),
genetics, prevalence/epidemiology, and treatments, all cited to the literature.
It has no API: content is one LinkML YAML file per disorder under
``kb/disorders/<Disorder>.yaml``. This module reads those files from GitHub (the
directory listing via the contents API, individual files via raw.githubusercontent)
and renders the large records down to the sections an agent asks for.

Mirrors ``literature.py`` / ``monarch.py``: ``requests`` with a timeout,
``raise_for_status``, defensive parsing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests
import yaml  # type: ignore[import-untyped]

_REPO = "monarch-initiative/dismech"
_BRANCH = "main"
CONTENTS_API = f"https://api.github.com/repos/{_REPO}/contents/kb/disorders?ref={_BRANCH}"
RAW_BASE = f"https://raw.githubusercontent.com/{_REPO}/{_BRANCH}/kb/disorders"

_RATE_LIMIT_SECONDS = 0.2
_DEFAULT_TIMEOUT = 20

# The heavy list sections a caller may want to trim to control output size. Order
# is the render order used by ``format_disorder_markdown``.
SECTION_KEYS = (
    "prevalence",
    "inheritance",
    "genetic",
    "pathophysiology",
    "mechanistic_hypotheses",
    "phenotypes",
    "treatments",
    "animal_models",
)


class DisMechError(RuntimeError):
    """Raised when a DisMech GitHub request fails after reaching the server."""


def list_disorders() -> list[str]:
    """Return the disorder file names available in DisMech (e.g. ``Achondroplasia.yaml``)."""
    response = requests.get(
        CONTENTS_API,
        headers={"Accept": "application/vnd.github+json"},
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    time.sleep(_RATE_LIMIT_SECONDS)
    items = response.json()
    if not isinstance(items, list):
        raise DisMechError("Unexpected DisMech contents response: not a JSON array")
    return sorted(
        item["name"]
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "file"
        and item.get("name", "").endswith(".yaml")
    )


def _candidate_filenames(name: str) -> list[str]:
    """Filename candidates for a user-supplied disorder name."""
    stem = name[:-5] if name.lower().endswith(".yaml") else name
    variants = {
        stem,
        stem.replace(" ", "_"),
        stem.replace("_", " "),
        stem.replace(" ", "_").title(),
    }
    return [f"{v}.yaml" for v in variants]


def _fetch_raw(filename: str) -> dict[str, Any] | None:
    """Fetch and parse one disorder YAML by exact filename; None on 404."""
    response = requests.get(f"{RAW_BASE}/{filename}", timeout=_DEFAULT_TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    time.sleep(_RATE_LIMIT_SECONDS)
    data = yaml.safe_load(response.text)
    if not isinstance(data, dict):
        raise DisMechError(f"Unexpected DisMech YAML for {filename}: not a mapping")
    return data


def get_disorder(name: str) -> dict[str, Any] | None:
    """Fetch a disorder record by name.

    Tries direct filename candidates first; on miss, lists the directory and does
    a case-insensitive substring match. Returns the parsed record, or None if no
    disorder matches (callers should surface close matches via :func:`find_disorders`).
    """
    for candidate in _candidate_filenames(name):
        data = _fetch_raw(candidate)
        if data is not None:
            return data

    matches = find_disorders(name)
    if len(matches) == 1:
        return _fetch_raw(matches[0])
    if matches:
        # Prefer an exact stem match if present among the fuzzy matches.
        target = name.lower().replace(" ", "_")
        for match in matches:
            if match[:-5].lower() == target:
                return _fetch_raw(match)
    return None


def find_disorders(query: str) -> list[str]:
    """Return disorder file names whose name contains ``query`` (case-insensitive)."""
    needle = query.lower().replace("_", " ").strip()
    results = []
    for filename in list_disorders():
        haystack = filename[:-5].lower().replace("_", " ")
        if needle in haystack:
            results.append(filename)
    return results


# --------------------------------------------------------------------------- #
# Rendering                                                                     #
# --------------------------------------------------------------------------- #

_Renderer = Callable[[list[Any]], list[str]]


def _term_label(obj: Any) -> str:
    """Extract a ``Label (CURIE)`` string from a DisMech term/object, best-effort."""
    if not isinstance(obj, dict):
        return str(obj)
    inner = obj.get("term")
    term = inner if isinstance(inner, dict) else obj
    label = term.get("label") or obj.get("preferred_term") or obj.get("name") or ""
    curie = term.get("id") or ""
    if label and curie:
        return f"{label} (`{curie}`)"
    return label or curie or ""


def _disease_curie(data: dict[str, Any]) -> tuple[str, str]:
    """Return (label, curie) for the disorder's MONDO disease term."""
    dterm = data.get("disease_term") or {}
    term = dterm.get("term") if isinstance(dterm, dict) else {}
    if isinstance(term, dict):
        return str(term.get("label") or data.get("name") or ""), str(term.get("id") or "")
    return str(data.get("name") or ""), ""


def _render_prevalence(items: list[Any]) -> list[str]:
    lines = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pop = it.get("population", "")
        pct = it.get("percentage") or it.get("prevalence_class") or ""
        note = it.get("notes", "")
        lines.append(f"- {pop}: {pct}" + (f" — {note}" if note else ""))
    return lines


def _render_named_list(items: list[Any], *, term_key: str | None = None) -> list[str]:
    """Render a list of {name/description, <term_key>} objects as bullets."""
    lines = []
    for it in items:
        if not isinstance(it, dict):
            continue
        term = (
            _term_label(it.get(term_key)) if term_key and isinstance(it.get(term_key), dict) else ""
        )
        head = it.get("name") or term or "(unnamed)"
        desc = it.get("description") or it.get("notes") or ""
        bullet = f"- **{head}**"
        if term and term != head:
            bullet += f" — {term}"
        if it.get("frequency"):
            bullet += f" [{it['frequency']}]"
        if desc:
            bullet += f"\n  {str(desc)[:400]}"
        lines.append(bullet)
    return lines


def format_disorder_markdown(
    data: dict[str, Any], *, sections: list[str] | None = None, max_items: int = 8
) -> str:
    """Render a DisMech disorder record as markdown, trimmed to ``sections``.

    ``sections`` limits which list sections are shown (default: all in
    ``SECTION_KEYS``); ``max_items`` caps entries per section to keep output
    manageable. The header (name, MONDO id, category, description) is always shown.
    """
    wanted = [s for s in (sections or SECTION_KEYS) if s in SECTION_KEYS]
    label, curie = _disease_curie(data)
    parts = [f"# {data.get('name', label)}"]
    if curie:
        parts.append(f"\nDisease term: {label} (`{curie}`)")
    if data.get("category"):
        parts.append(f"\nCategory: {data['category']}")
    if data.get("description"):
        parts.append(f"\n\n{str(data['description'])[:1200]}")

    renderers: dict[str, tuple[str, _Renderer]] = {
        "prevalence": ("Prevalence & epidemiology", _render_prevalence),
        "inheritance": (
            "Inheritance",
            lambda v: _render_named_list(v, term_key="inheritance_term"),
        ),
        "genetic": ("Genetics", lambda v: _render_named_list(v, term_key="gene_term")),
        "pathophysiology": ("Pathophysiology", _render_named_list),
        "mechanistic_hypotheses": ("Mechanistic hypotheses", _render_named_list),
        "phenotypes": ("Phenotypes", lambda v: _render_named_list(v, term_key="phenotype_term")),
        "treatments": ("Treatments", lambda v: _render_named_list(v, term_key="treatment_term")),
        "animal_models": ("Animal models", _render_named_list),
    }

    for key in wanted:
        value = data.get(key)
        if not isinstance(value, list) or not value:
            continue
        title, render = renderers[key]
        rendered = render(value[:max_items])
        if not rendered:
            continue
        parts.append(f"\n\n## {title}\n" + "\n".join(rendered))
        if len(value) > max_items:
            parts.append(f"\n_(+{len(value) - max_items} more {key} entries)_")

    return "".join(parts)


def format_disorder_list_markdown(query: str, filenames: list[str]) -> str:
    """Render a disorder-name list as markdown for the agent."""
    if not filenames:
        return (
            f"No DisMech disorders found matching '{query}'."
            if query
            else "No DisMech disorders found."
        )
    names = [f.rsplit(".yaml", 1)[0].replace("_", " ") for f in filenames]
    header = (
        f"{len(names)} DisMech disorder(s)"
        + (f" matching '{query}'" if query else "")
        + " (pass a name to `get_dismech_disorder`):\n"
    )
    return header + "\n".join(f"- {n}" for n in names)

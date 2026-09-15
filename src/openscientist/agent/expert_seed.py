"""Expert seed helper.

Parses vendored prompts into SeedRow dataclasses for the Alembic seed
migration.  Uses ``ON CONFLICT (slug) DO NOTHING`` for idempotency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Expert

# Foreign MCP tool references are stripped at seed time; only our server passes through.
_OPENSCIENTIST_MCP_PREFIX: Final[str] = "mcp__openscientist-tools__"

VENDORED_ROOT: Final[Path] = Path(__file__).resolve().parent / "prompts" / "thirdparty"

# (source, filename, slug, category)
EXPECTED_VENDORED: Final[list[tuple[str, str, str, str]]] = [
    ("anthropic", "research_lead_agent.md", "research-lead", "research"),
    ("anthropic", "research_subagent.md", "research-subagent", "research"),
    ("anthropic", "citations_agent.md", "citations-agent", "methodology"),
    ("wshobson", "data-scientist.md", "data-scientist", "methodology"),
    ("wshobson", "python-pro.md", "python-pro", "code"),
    (
        "voltagent",
        "scientific-literature-researcher.md",
        "scientific-literature-researcher",
        "research",
    ),
    ("voltagent", "data-researcher.md", "data-researcher", "research"),
    ("voltagent", "research-analyst.md", "research-analyst", "research"),
]

_SOURCE_URL_TEMPLATES: Final[dict[str, str]] = {
    "anthropic": (
        "https://github.com/anthropics/claude-cookbooks/blob/main/"
        "patterns/agents/prompts/{filename}"
    ),
    "wshobson": (
        "https://github.com/wshobson/agents/blob/main/plugins/"
        "machine-learning-ops/agents/{filename}"
    ),
    "voltagent": (
        "https://github.com/VoltAgent/awesome-claude-code-subagents/blob/main/"
        "categories/10-research-analysis/{filename}"
    ),
}

_WSHOBSON_OVERRIDES: Final[dict[str, str]] = {
    "python-pro.md": (
        "https://github.com/wshobson/agents/blob/main/plugins/"
        "python-development/agents/python-pro.md"
    ),
}


@dataclass(frozen=True)
class SeedRow:
    """Pre-insert row data for the ``experts`` table (no ORM dependency)."""

    slug: str
    name: str
    description: str
    prompt: str
    tools: list[str] | None
    model: str | None
    category: str
    source: str
    source_url: str | None


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body from a markdown file."""
    if not text.startswith("---"):
        raise ValueError("File does not start with YAML frontmatter fence")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if match is None:
        raise ValueError("Frontmatter block is malformed")
    block, body = match.group(1), match.group(2)

    loaded = yaml.safe_load(block)
    if loaded is None:
        return {}, body
    if not isinstance(loaded, dict):
        raise ValueError(f"Frontmatter is not a mapping: {type(loaded).__name__}")
    return {str(k): v for k, v in loaded.items()}, body


def _parse_tools_field(raw: Any) -> list[str] | None:
    """Normalize tools to list[str], dropping foreign MCP references."""
    if raw is None:
        return None
    if isinstance(raw, list):
        candidates = [str(t).strip() for t in raw if str(t).strip()]
    elif isinstance(raw, str):
        candidates = [t.strip() for t in raw.split(",") if t.strip()]
    else:
        raise ValueError(f"Unexpected `tools` field type: {type(raw).__name__}")

    kept: list[str] = []
    for tool in candidates:
        if tool.startswith("mcp__") and not tool.startswith(_OPENSCIENTIST_MCP_PREFIX):
            continue
        kept.append(tool)
    return kept or None


def _build_source_url(source: str, filename: str) -> str:
    override = _WSHOBSON_OVERRIDES.get(filename) if source == "wshobson" else None
    if override is not None:
        return override
    template = _SOURCE_URL_TEMPLATES[source]
    return template.format(filename=filename)


def parse_vendored_file(path: Path, *, source: str, slug: str) -> SeedRow:
    """Parse a vendored markdown file into a SeedRow."""
    text = path.read_text(encoding="utf-8")
    fields, body = _split_frontmatter(text)

    name = str(fields.get("name") or slug)
    description = str(fields.get("description") or "")
    tools = _parse_tools_field(fields.get("tools"))
    # All seeded experts inherit the orchestrator's model (upstream pins discarded).
    model: str = "inherit"
    category = _lookup_category(source, path.name, slug)

    filename = path.name
    source_url = _build_source_url(source, filename)

    return SeedRow(
        slug=slug,
        name=name,
        description=description,
        prompt=body.strip(),
        tools=tools,
        model=model,
        category=category,
        source=source,
        source_url=source_url,
    )


def _lookup_category(source: str, filename: str, slug: str) -> str:
    for s, f, sl, cat in EXPECTED_VENDORED:
        if s == source and f == filename and sl == slug:
            return cat
    raise ValueError(f"No category in contract for ({source!r}, {filename!r}, {slug!r})")


def all_seed_rows() -> list[SeedRow]:
    """Parse all vendored files into seed rows."""
    rows: list[SeedRow] = []
    for source, filename, slug, _category in EXPECTED_VENDORED:
        path = VENDORED_ROOT / source / filename
        rows.append(parse_vendored_file(path, source=source, slug=slug))
    return rows


async def run_seed(session: AsyncSession) -> None:
    """Insert all vendored seed rows idempotently (ON CONFLICT DO NOTHING).

    None-valued nullable fields are omitted so JSONB ``null`` does not
    violate the CHECK constraint that expects SQL NULL or a JSON array.
    """
    for row in all_seed_rows():
        values: dict[str, object] = {
            "slug": row.slug,
            "name": row.name,
            "description": row.description,
            "prompt": row.prompt,
            "category": row.category,
            "source": row.source,
            "is_enabled": True,
        }
        if row.tools is not None:
            values["tools"] = row.tools
        if row.model is not None:
            values["model"] = row.model
        if row.source_url is not None:
            values["source_url"] = row.source_url
        stmt = insert(Expert).values(**values).on_conflict_do_nothing(index_elements=["slug"])
        await session.execute(stmt)

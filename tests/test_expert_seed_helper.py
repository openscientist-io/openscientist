"""Unit tests for the expert seed helper (pure-function, no DB)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openscientist.agent.expert_seed import (
    SeedRow,
    all_seed_rows,
    parse_vendored_file,
)

_ANTHROPIC_CITATIONS = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "openscientist"
    / "agent"
    / "prompts"
    / "thirdparty"
    / "anthropic"
    / "citations_agent.md"
)


def test_parse_returns_seed_row() -> None:
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert isinstance(row, SeedRow)


def test_parse_extracts_slug_from_argument() -> None:
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert row.slug == "citations-agent"


def test_parse_extracts_name_from_frontmatter() -> None:
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert row.name == "citations-agent"


def test_parse_extracts_description_from_frontmatter() -> None:
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert "citation" in row.description.lower()
    assert len(row.description) > 20


def test_parse_body_excludes_frontmatter() -> None:
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert not row.prompt.startswith("---")
    assert "You are an agent for adding correct citations" in row.prompt


def test_parse_fills_source() -> None:
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert row.source == "anthropic"


def test_parse_fills_source_url() -> None:
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert row.source_url is not None
    assert "anthropics/claude-cookbooks" in row.source_url
    assert row.source_url.endswith("citations_agent.md")


def test_parse_sets_model_inherit() -> None:
    """All vendored experts inherit the orchestrator's model by default."""
    row = parse_vendored_file(_ANTHROPIC_CITATIONS, source="anthropic", slug="citations-agent")
    assert row.model == "inherit" or row.model is None


def test_parse_raises_on_missing_file(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist.md"
    with pytest.raises((FileNotFoundError, OSError)):
        parse_vendored_file(bogus, source="anthropic", slug="bogus")


def test_all_seed_rows_returns_exactly_eight() -> None:
    rows = all_seed_rows()
    assert len(rows) == 8


def test_all_seed_rows_slugs_are_unique() -> None:
    rows = all_seed_rows()
    slugs = [r.slug for r in rows]
    assert len(slugs) == len(set(slugs))


def test_all_seed_rows_cover_three_sources() -> None:
    rows = all_seed_rows()
    sources = {r.source for r in rows}
    assert sources == {"anthropic", "wshobson", "voltagent"}


def test_all_seed_rows_have_non_empty_fields() -> None:
    """Every seeded row must have slug, name, description, prompt, category, source."""
    for row in all_seed_rows():
        assert row.slug, f"Empty slug: {row}"
        assert row.name, f"Empty name in {row.slug}"
        assert row.description, f"Empty description in {row.slug}"
        assert row.prompt, f"Empty prompt in {row.slug}"
        assert row.category, f"Empty category in {row.slug}"
        assert row.source, f"Empty source in {row.slug}"


def test_all_seed_rows_have_attribution() -> None:
    """Every third-party row must carry source_url."""
    for row in all_seed_rows():
        assert row.source_url, f"Missing source_url in {row.slug}"


def test_parse_strips_quoted_description() -> None:
    """YAML-quoted descriptions must not retain surrounding quotes."""
    from openscientist.agent.expert_seed import VENDORED_ROOT, parse_vendored_file

    row = parse_vendored_file(
        VENDORED_ROOT / "voltagent" / "research-analyst.md",
        source="voltagent",
        slug="research-analyst",
    )
    assert not row.description.startswith('"'), (
        f"description retained leading quote: {row.description[:30]!r}"
    )
    assert not row.description.endswith('"'), (
        f"description retained trailing quote: {row.description[-30:]!r}"
    )
    assert "comprehensive research" in row.description


def test_parse_strips_foreign_mcp_tools() -> None:
    """Foreign MCP tool references are stripped from the tools list."""
    from openscientist.agent.expert_seed import VENDORED_ROOT, parse_vendored_file

    row = parse_vendored_file(
        VENDORED_ROOT / "voltagent" / "scientific-literature-researcher.md",
        source="voltagent",
        slug="scientific-literature-researcher",
    )
    assert row.tools is not None
    for tool in row.tools:
        if tool.startswith("mcp__"):
            assert tool.startswith("mcp__openscientist-tools__"), (
                f"Foreign MCP tool leaked into seed row: {tool!r}"
            )
    assert "Read" in row.tools


def test_parse_tools_as_yaml_list() -> None:
    """YAML list syntax for tools is accepted."""
    from openscientist.agent.expert_seed import VENDORED_ROOT, parse_vendored_file

    row = parse_vendored_file(
        VENDORED_ROOT / "voltagent" / "data-researcher.md",
        source="voltagent",
        slug="data-researcher",
    )
    assert row.tools is not None
    assert len(row.tools) >= 2
    for tool in row.tools:
        assert "," not in tool, f"tools entry still joined by commas: {tool!r}"


def test_all_seed_rows_inherit_orchestrator_model() -> None:
    """Every seeded expert uses model='inherit'."""
    for row in all_seed_rows():
        assert row.model == "inherit", f"{row.slug}: expected model='inherit', got {row.model!r}"

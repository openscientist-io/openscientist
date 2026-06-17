"""Tests for delivering skills to the codex/Ollama agent as native SKILL.md."""

from __future__ import annotations

import yaml  # type: ignore[import-untyped]

from openscientist.agent.skills import codex_skill_markdown
from openscientist.database.models import Skill


def _parse_frontmatter(skill_md: str) -> tuple[dict[str, object], str]:
    """Split a SKILL.md into its YAML frontmatter dict and the body."""
    assert skill_md.startswith("---\n")
    _, fm, body = skill_md.split("---\n", 2)
    return yaml.safe_load(fm), body


def test_codex_skill_markdown_basic() -> None:
    skill = Skill(
        name="Pathway Enrichment",
        slug="pathway-enrichment",
        category="metabolomics",
        description="Run pathway enrichment analysis on differential metabolites.",
        content="# Pathway Enrichment\n\nStep 1: ...\n",
        is_enabled=True,
    )
    md = codex_skill_markdown(skill)
    fm, body = _parse_frontmatter(md)
    assert fm["name"] == "metabolomics--pathway-enrichment"
    assert fm["description"] == "Run pathway enrichment analysis on differential metabolites."
    # Body is the skill content verbatim.
    assert body == skill.content


def test_codex_skill_markdown_null_description_gets_fallback() -> None:
    """Codex drops skills with an empty description, so a null description must
    fall back to a non-empty string."""
    skill = Skill(
        name="Bare Skill",
        slug="bare",
        category="genomics",
        description=None,
        content="body",
        is_enabled=True,
    )
    fm, _ = _parse_frontmatter(codex_skill_markdown(skill))
    assert fm["description"]  # non-empty
    assert "genomics" in str(fm["description"])


def test_codex_skill_markdown_truncates_name_and_description() -> None:
    skill = Skill(
        name="Long",
        slug="s" * 80,
        category="c" * 40,
        description="d " * 800,  # >1024 chars before collapsing
        content="x",
        is_enabled=True,
    )
    fm, _ = _parse_frontmatter(codex_skill_markdown(skill))
    assert len(str(fm["name"])) <= 64
    assert len(str(fm["description"])) <= 1024
    # Description is single-line (collapsed whitespace).
    assert "\n" not in str(fm["description"])


def test_codex_skill_markdown_handles_yaml_special_chars() -> None:
    """A colon or quote in name/description must not break frontmatter parsing."""
    skill = Skill(
        name="Tricky",
        slug="tricky",
        category="meta",
        description='Analysis: uses "quotes" and: colons',
        content="body",
        is_enabled=True,
    )
    fm, _ = _parse_frontmatter(codex_skill_markdown(skill))
    assert fm["description"] == 'Analysis: uses "quotes" and: colons'

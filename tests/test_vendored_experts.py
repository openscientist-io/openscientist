"""Tests that vendored third-party expert prompts are present and well-formed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Location of vendored prompts on disk.
VENDORED_ROOT: Path = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "openscientist"
    / "agent"
    / "prompts"
    / "thirdparty"
)

# (source_dir, filename, expected_slug)
EXPECTED_VENDORED: list[tuple[str, str, str]] = [
    ("anthropic", "research_lead_agent.md", "research-lead"),
    ("anthropic", "research_subagent.md", "research-subagent"),
    ("anthropic", "citations_agent.md", "citations-agent"),
    ("wshobson", "data-scientist.md", "data-scientist"),
    ("wshobson", "python-pro.md", "python-pro"),
    (
        "voltagent",
        "scientific-literature-researcher.md",
        "scientific-literature-researcher",
    ),
    ("voltagent", "data-researcher.md", "data-researcher"),
    ("voltagent", "research-analyst.md", "research-analyst"),
]

EXPECTED_SOURCES: list[str] = ["anthropic", "wshobson", "voltagent"]


def _vendored_path(source: str, filename: str) -> Path:
    return VENDORED_ROOT / source / filename


def _split_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Extract YAML frontmatter and body, or None if no frontmatter."""
    if not text.startswith("---"):
        return None
    # Find the closing fence.  Start searching after the opening ---.
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if match is None:
        return None
    frontmatter_block = match.group(1)
    body = match.group(2)

    fields: dict[str, str] = {}
    for line in frontmatter_block.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body


def test_vendored_root_directory_exists() -> None:
    """The thirdparty root directory must exist."""
    assert VENDORED_ROOT.is_dir(), (
        f"Vendored root not found at {VENDORED_ROOT}. "
        f"Phase 5 expects prompts to live under this path."
    )


@pytest.mark.parametrize("source", EXPECTED_SOURCES)
def test_each_source_directory_exists(source: str) -> None:
    """Each upstream source has its own subdirectory."""
    assert (VENDORED_ROOT / source).is_dir()


@pytest.mark.parametrize("source,filename,_slug", EXPECTED_VENDORED)
def test_vendored_file_exists(source: str, filename: str, _slug: str) -> None:
    """Each expected vendored markdown file exists."""
    path = _vendored_path(source, filename)
    assert path.is_file(), f"Missing vendored file: {path}"


@pytest.mark.parametrize("source,filename,_slug", EXPECTED_VENDORED)
def test_vendored_file_has_yaml_frontmatter(source: str, filename: str, _slug: str) -> None:
    """Each vendored file must have a parseable YAML frontmatter block."""
    path = _vendored_path(source, filename)
    text = path.read_text(encoding="utf-8")
    parsed = _split_frontmatter(text)
    assert parsed is not None, f"No YAML frontmatter in {path}"


@pytest.mark.parametrize("source,filename,_slug", EXPECTED_VENDORED)
def test_vendored_frontmatter_has_name_and_description(
    source: str, filename: str, _slug: str
) -> None:
    """Frontmatter must carry non-empty ``name`` and ``description``."""
    path = _vendored_path(source, filename)
    text = path.read_text(encoding="utf-8")
    parsed = _split_frontmatter(text)
    assert parsed is not None, f"No YAML frontmatter in {path}"
    fields, _ = parsed
    assert fields.get("name"), f"Missing or empty `name` in {path}"
    assert fields.get("description"), f"Missing or empty `description` in {path}"


@pytest.mark.parametrize("source,filename,_slug", EXPECTED_VENDORED)
def test_vendored_body_is_substantial(source: str, filename: str, _slug: str) -> None:
    """The post-frontmatter body must be a real system prompt, not a stub."""
    path = _vendored_path(source, filename)
    text = path.read_text(encoding="utf-8")
    parsed = _split_frontmatter(text)
    assert parsed is not None, f"No YAML frontmatter in {path}"
    _, body = parsed
    assert len(body.strip()) >= 200, (
        f"Vendored body in {path} is too short ({len(body.strip())} chars); expected at least 200"
    )


def test_expected_slugs_are_unique() -> None:
    """The contract slugs must be unique (will become DB primary keys)."""
    slugs = [slug for (_, _, slug) in EXPECTED_VENDORED]
    assert len(slugs) == len(set(slugs)), f"Duplicate slugs in contract: {slugs}"


def test_exactly_eight_vendored_files_expected() -> None:
    """Contract size is fixed at 8 per the plan."""
    assert len(EXPECTED_VENDORED) == 8

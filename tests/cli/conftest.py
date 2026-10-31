"""Fixtures for the CLI tests: a temp checkout with the example env files."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
EXAMPLE_FILES = (".env.example", ".env.db.example", ".env.owner.example")


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway checkout: the example env files, with the cwd moved into it."""
    for name in EXAMPLE_FILES:
        (tmp_path / name).write_text((REPO_ROOT / name).read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


# The full scripted wizard run for the default provider: provider 1 (Anthropic),
# an API key, then every remaining question at its default.
ANTHROPIC_SETUP_ANSWERS = "1\nsk-test-123\n\n\n\n\n\n\n\n"


def env_value(checkout: Path, file: str, key: str) -> str:
    from openscientist.cli import envfiles

    return envfiles.parse_values((checkout / file).read_text(encoding="utf-8").splitlines()).get(
        key, ""
    )

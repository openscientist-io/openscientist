"""Fixtures for the CLI tests: a temp checkout with the example env files."""

from pathlib import Path

import pytest

from openscientist.providers.base import LiveCheckResult

REPO_ROOT = Path(__file__).parents[2]
EXAMPLE_FILES = (".env.example", ".env.db.example", ".env.owner.example")


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway checkout: the example env files, with the cwd moved into it."""
    for name in EXAMPLE_FILES:
        (tmp_path / name).write_text((REPO_ROOT / name).read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def fake_live_check(monkeypatch):
    """No test reaches a real provider; the live check passes unless a test overrides it."""
    from openscientist.cli import livecheck

    monkeypatch.setattr(
        livecheck,
        "perform_live_check",
        lambda provider_id, answers: LiveCheckResult(ok=True, detail="ok"),
    )


def env_value(checkout: Path, file: str, key: str) -> str:
    from openscientist.cli import envfiles

    return envfiles.parse_values((checkout / file).read_text(encoding="utf-8").splitlines()).get(
        key, ""
    )


def wizard_tail(dev_mode: str = "true", bind_address: str = "") -> list[str]:
    """The answers to every step after the provider's own questions.

    Address, harness and concurrency at their defaults, no OAuth client, the dev
    mock login on, air-gapped off, no Phenix.
    """
    return [
        "",  # OPENSCIENTIST_BASE_URL
        "",  # PORT
        bind_address,  # OPENSCIENTIST_BIND_ADDRESS
        "",  # POSTGRES_PORT
        "",  # OPENSCIENTIST_HARNESS
        "",  # OPENSCIENTIST_MAX_CONCURRENT_JOBS
        "",  # GOOGLE_CLIENT_ID
        "",  # GOOGLE_CLIENT_SECRET
        "",  # GITHUB_CLIENT_ID
        "",  # GITHUB_CLIENT_SECRET
        "",  # ORCID_CLIENT_ID
        "",  # ORCID_CLIENT_SECRET
        dev_mode,  # OPENSCIENTIST_DEV_MODE
        "",  # BOOTSTRAP_ADMIN_EMAILS
        "",  # OPENSCIENTIST_AIRGAPPED
        "",  # PHENIX_HOST_PATH
    ]


def wizard_input(
    provider_choice: str, provider_answers: list[str], *, dev_mode: str = "true"
) -> str:
    """One scripted wizard run: the provider choice, its questions, then the tail."""
    return "\n".join([provider_choice, *provider_answers, *wizard_tail(dev_mode)]) + "\n"


# The full scripted wizard run for the default provider (Anthropic): the API key,
# then every remaining question at its default (dev mock login on).
ANTHROPIC_SETUP_ANSWERS = wizard_input("1", ["sk-test-123", "", ""])

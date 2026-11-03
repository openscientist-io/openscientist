"""A container run's LLM proxy and execution broker credentials work only while
that run lives (plans/token-findings.md)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol
from uuid import uuid4

import pytest

from openscientist.job_container.secrets import (
    RunCredentials,
    RunKind,
    broker_accepts,
    issue_run_credentials,
    proxy_accepts,
    proxy_run,
    revoke_run_credentials,
)


class StartRun(Protocol):
    """Starts a run of a job, returning its login role and credentials."""

    def __call__(self, job_id: str, kind: RunKind = "discovery") -> tuple[str, RunCredentials]: ...


@pytest.fixture
def start_run() -> Iterator[StartRun]:
    """Start a run of a job, returning its login role and credentials."""
    roles: list[str] = []

    def start(job_id: str, kind: RunKind = "discovery") -> tuple[str, RunCredentials]:
        role = f"agent_{uuid4().hex}"
        roles.append(role)
        return role, issue_run_credentials(job_id, role, kind)

    yield start
    for role in roles:
        revoke_run_credentials(role)


def _accepted(job_id: str, credentials: RunCredentials) -> tuple[bool, bool]:
    return (
        proxy_accepts(credentials.llm_placeholder),
        broker_accepts(job_id, credentials.exec_token),
    )


def test_a_runs_credentials_end_with_it(start_run: StartRun) -> None:
    role, credentials = start_run("job-1")
    assert _accepted("job-1", credentials) == (True, True)
    revoke_run_credentials(role)
    assert _accepted("job-1", credentials) == (False, False)


def test_an_earlier_run_of_the_same_job_stays_refused(start_run: StartRun) -> None:
    first_role, first = start_run("job-1")
    revoke_run_credentials(first_role)
    _, second = start_run("job-1")
    assert _accepted("job-1", first) == (False, False)
    assert _accepted("job-1", second) == (True, True)


def test_two_live_runs_of_one_job_end_separately(start_run: StartRun) -> None:
    chat_role, chat = start_run("job-1")
    _, discovery = start_run("job-1")
    revoke_run_credentials(chat_role)
    assert _accepted("job-1", chat) == (False, False)
    assert _accepted("job-1", discovery) == (True, True)


def test_a_broker_token_works_only_for_its_own_job(start_run: StartRun) -> None:
    _, credentials = start_run("job-1")
    assert not broker_accepts("job-2", credentials.exec_token)


def test_credentials_made_up_for_a_job_are_refused() -> None:
    made_up = "job-1." + "0" * 64
    assert not proxy_accepts(made_up)
    assert not broker_accepts("job-1", made_up)


def test_the_proxy_resolves_a_token_to_its_run(start_run: StartRun) -> None:
    """Attribution: the proxy reads the presented token off the live run."""
    role, credentials = start_run("job-7")
    assert role is not None
    run = proxy_run(credentials.llm_placeholder)
    assert run is not None
    assert run.job_id == "job-7"
    assert run.kind == "discovery"
    assert run.llm_placeholder == credentials.llm_placeholder


@pytest.mark.parametrize("kind", ["discovery", "report", "chat"])
def test_the_run_records_its_kind_at_issuance(start_run: StartRun, kind: str) -> None:
    _, credentials = start_run("job-9", kind=kind)  # type: ignore[arg-type]
    assert proxy_run(credentials.llm_placeholder) is not None
    assert proxy_run(credentials.llm_placeholder).kind == kind  # type: ignore[union-attr]


def test_the_proxy_resolves_nothing_for_a_foreign_or_ended_token(
    start_run: StartRun,
) -> None:
    role, credentials = start_run("job-1")
    assert proxy_run("job-1." + "0" * 64) is None
    assert proxy_run("") is None
    revoke_run_credentials(role)
    assert proxy_run(credentials.llm_placeholder) is None

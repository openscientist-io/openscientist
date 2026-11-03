"""Secrets for the untrusted agent container: its derived settings key, and the
credentials one container run presents to the LLM proxy and the execution broker."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from typing import Literal

#: The work one container run does, recorded at credential issuance so the
#: proxy can attribute a forwarded call without reading the database.
RunKind = Literal["discovery", "report", "chat"]

_JOB_SECRET_LABEL = "job_secret:"
_SEP = "."


def derive_job_secret(master_key: str, job_id: str) -> str:
    """Return HMAC-SHA256(master_key, "job_secret:" + job_id) as a 64-char hex digest.

    Keeps the master key out of the job container while satisfying the required
    Settings.secret_key field inside it.
    """
    message = f"{_JOB_SECRET_LABEL}{job_id}".encode()
    return hmac.new(master_key.encode(), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class RunCredentials:
    """What one container run presents to the LLM proxy and to the execution
    broker, each ``"<job_id>.<random token>"``, for the run of ``job_id``
    whose kind is ``kind`` (discovery, report or chat)."""

    job_id: str
    kind: RunKind
    llm_placeholder: str
    exec_token: str


# Kept by the web process, which runs the proxy, the broker and every container
# launch, so a restart, which ends every run, forgets them all.
_runs: dict[str, RunCredentials] = {}
_runs_lock = threading.Lock()


def issue_run_credentials(job_id: str, role: str, kind: RunKind) -> RunCredentials:
    """New credentials for the run of ``job_id`` whose database login is ``role``
    and whose kind is ``kind``, accepted until :func:`revoke_run_credentials`
    ends them."""
    credentials = RunCredentials(
        job_id=job_id,
        kind=kind,
        llm_placeholder=f"{job_id}{_SEP}{secrets.token_hex(32)}",
        exec_token=f"{job_id}{_SEP}{secrets.token_hex(32)}",
    )
    with _runs_lock:
        _runs[role] = credentials
    return credentials


def revoke_run_credentials(role: str) -> None:
    """End the credentials of the run whose database login is ``role``."""
    with _runs_lock:
        _runs.pop(role, None)


def _live_runs() -> list[RunCredentials]:
    with _runs_lock:
        return list(_runs.values())


def _same(expected: str, presented: str) -> bool:
    return hmac.compare_digest(expected.encode(), presented.encode())


def proxy_run(placeholder: str) -> RunCredentials | None:
    """The live run whose LLM credential ``placeholder`` is, or None.

    Sibling of :func:`proxy_accepts`: it resolves the presented token to the
    run it belongs to, so the proxy attributes a forwarded call to its job
    and kind without reading the database."""
    for run in _live_runs():
        if _same(run.llm_placeholder, placeholder):
            return run
    return None


def proxy_accepts(placeholder: str) -> bool:
    """Whether ``placeholder`` is the LLM credential of a live run."""
    return proxy_run(placeholder) is not None


def broker_accepts(job_id: str, token: str) -> bool:
    """Whether ``token`` is the execution credential of a live run of ``job_id``."""
    if token.rpartition(_SEP)[0] != job_id:
        return False
    return any(_same(run.exec_token, token) for run in _live_runs())

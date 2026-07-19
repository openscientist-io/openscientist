"""Per-job secret derivation for the untrusted agent container."""

from __future__ import annotations

import hashlib
import hmac

_JOB_SECRET_LABEL = "job_secret:"
_EXEC_TOKEN_LABEL = "exec_token:"
_PLACEHOLDER_SEP = "."


def derive_job_secret(master_key: str, job_id: str) -> str:
    """Return HMAC-SHA256(master_key, "job_secret:" + job_id) as a 64-char hex digest.

    Keeps the master key out of the job container while satisfying the required
    Settings.secret_key field inside it.
    """
    message = f"{_JOB_SECRET_LABEL}{job_id}".encode()
    return hmac.new(master_key.encode(), message, hashlib.sha256).hexdigest()


def derive_exec_token(master_key: str, job_id: str) -> str:
    """Per-job token the execution broker verifies, keyed by a distinct label."""
    message = f"{_EXEC_TOKEN_LABEL}{job_id}".encode()
    return hmac.new(master_key.encode(), message, hashlib.sha256).hexdigest()


def make_exec_placeholder(master_key: str, job_id: str) -> str:
    """Execution credential "<job_id>.<token>" the broker recomputes and verifies."""
    return f"{job_id}{_PLACEHOLDER_SEP}{derive_exec_token(master_key, job_id)}"

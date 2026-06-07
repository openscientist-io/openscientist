"""Air-gap credential verifier — startup gate (RFC §12.3).

Where :mod:`airgap.env_allowlist` is the *filter* (constructs the agent env
with only the active provider's credentials) and :mod:`airgap.export_boundary`
is the *exit gate* (refuses artifact export if a secret slipped through to
the report), this module is the *entry gate*: at job start, verify the
filter actually worked. If an inactive-provider credential, master secret,
or other forbidden value is present in the env that's about to reach the
agent container — or sitting in the freshly-prepared ``job_dir`` — refuse
to start the job.

The two paths complement each other:

* :func:`verify_env` walks the env dict, skips the active provider's own
  credentials (those are *supposed* to be there), and applies the same
  high-precision shape rules :mod:`export_boundary` uses to *values* of
  every other var. Catches a regression in :func:`env_allowlist.filtered_agent_env`
  that lets a cross-provider credential through.
* :func:`verify_job_dir` runs the export-boundary scanner against the
  job_dir at job start (when it should be near-empty). Anything matched
  here is either left over from a prior failed run or smuggled in by a
  caller that bypassed the filter — both are reasons to refuse.

The :class:`StartupVerificationResult` returned by :func:`verify_airgap_startup`
is what the orchestrator checks before invoking ``get_agent(config).run()``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from openscientist.airgap.env_allowlist import PROVIDER_ENV_VARS
from openscientist.airgap.export_boundary import (
    DEFAULT_SECRET_RULES,
    SecretFinding,
    SecretRule,
    Severity,
    _redacted_context,
    scan_for_secrets,
)

logger = logging.getLogger(__name__)


# Env-var names that must never be present in the agent container env in
# air-gap mode, regardless of value. The master secret and the full DB URL
# are infrastructure secrets the agent never legitimately needs; if the
# env_allowlist worked they're already stripped, and seeing them here means
# the filter was bypassed.
FORBIDDEN_ENV_VARS: frozenset[str] = frozenset(
    {
        "OPENSCIENTIST_SECRET_KEY",
        "DATABASE_URL",
    }
)


@dataclass
class EnvFinding:
    """One forbidden value detected in the agent env."""

    var_name: str
    rule_name: str
    severity: Severity
    context: str  # ±40 chars around the match, with the value REDACTED
    description: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "var_name": self.var_name,
            "rule": self.rule_name,
            "severity": self.severity,
            "context": self.context,
            "description": self.description,
        }


# ----------------------------------------------------------------- env scan


def verify_env(
    env: dict[str, str],
    active_provider_id: str,
    rules: list[SecretRule] | None = None,
) -> list[EnvFinding]:
    """Scan the agent env for forbidden values.

    Two checks run in order:

    1. Any var whose name is in :data:`FORBIDDEN_ENV_VARS` (master secret,
       full DB URL) is flagged regardless of value. Severity ``block``.
    2. Every other var (except the active provider's own credentials,
       which are supposed to be present) has its **value** scanned against
       the secret-shape rule set. A match means the env_allowlist didn't
       strip a cross-provider credential.

    Args:
        env: The container env about to be passed to the agent (typically
            the return of :func:`env_allowlist.filtered_agent_env`).
        active_provider_id: Provider id whose own credentials should be
            exempted from the value scan.
        rules: Override the secret ruleset. Defaults to
            :data:`export_boundary.DEFAULT_SECRET_RULES`.
    """
    rules = rules if rules is not None else DEFAULT_SECRET_RULES
    allowed_for_provider = PROVIDER_ENV_VARS.get(active_provider_id, frozenset())
    findings: list[EnvFinding] = []

    for var_name, value in env.items():
        # (1) forbidden by name regardless of value
        if var_name in FORBIDDEN_ENV_VARS:
            findings.append(
                EnvFinding(
                    var_name=var_name,
                    rule_name="forbidden-env-var",
                    severity="block",
                    context="<REDACTED>",
                    description=(
                        f"{var_name} must not be present in the air-gap "
                        "agent env (env_allowlist filter bypassed?)."
                    ),
                )
            )
            continue
        # The active provider's own credentials *are* allowed to be present
        # and carry secret-shaped values; skip them.
        if var_name in allowed_for_provider:
            continue
        if not value:
            continue
        # (2) value shape scan — first match wins (one finding per var keeps
        # the report compact; the rule list is ordered most-specific-first).
        for rule in rules:
            match = rule.pattern.search(value)
            if match:
                findings.append(
                    EnvFinding(
                        var_name=var_name,
                        rule_name=rule.name,
                        severity=rule.severity,
                        context=_redacted_context(value, match),
                        description=rule.description,
                    )
                )
                break
    return findings


# ----------------------------------------------------------------- job_dir scan


def verify_job_dir(
    job_dir: Path,
    rules: list[SecretRule] | None = None,
) -> list[SecretFinding]:
    """Scan the job_dir at job start for any secret-shaped content.

    At job start the directory should contain the prompt, the input data
    files, and not much else; any rule match here is either residue from a
    prior run or a caller that bypassed the env filter. Delegates to
    :func:`export_boundary.scan_for_secrets` so the ruleset and skip
    heuristics (binary, size cap, missing-file tolerance) stay consistent
    across the begin/end gates.
    """
    if not job_dir.exists():
        return []
    return scan_for_secrets([job_dir], rules)


# ----------------------------------------------------------------- top-level


@dataclass
class StartupVerificationResult:
    """Combined result of :func:`verify_airgap_startup`.

    The orchestrator reads :attr:`passed`: True → proceed; False → refuse
    to start the agent and surface :attr:`blocking_findings` to the operator.
    """

    passed: bool
    env_findings: list[EnvFinding] = field(default_factory=list)
    file_findings: list[SecretFinding] = field(default_factory=list)

    @property
    def blocking_env(self) -> list[EnvFinding]:
        return [f for f in self.env_findings if f.severity == "block"]

    @property
    def blocking_files(self) -> list[SecretFinding]:
        return [f for f in self.file_findings if f.severity == "block"]

    @property
    def blocking_count(self) -> int:
        return len(self.blocking_env) + len(self.blocking_files)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.env_findings if f.severity == "warn") + sum(
            1 for f in self.file_findings if f.severity == "warn"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "blocking_count": self.blocking_count,
            "warning_count": self.warning_count,
            "env_findings": [f.as_dict() for f in self.env_findings],
            "file_findings": [f.as_dict() for f in self.file_findings],
        }


def verify_airgap_startup(
    env: dict[str, str],
    active_provider_id: str,
    job_dir: Path,
    rules: list[SecretRule] | None = None,
) -> StartupVerificationResult:
    """Run env + job_dir scans; return the combined startup decision.

    ``passed`` is True iff there are zero **block**-severity findings.
    Warnings (e.g. a 32-char hex value that *might* be an Azure key but
    might also be a hash) are recorded but don't block.
    """
    env_findings = verify_env(env, active_provider_id, rules)
    file_findings = verify_job_dir(job_dir, rules)
    has_blocking = any(f.severity == "block" for f in env_findings) or any(
        f.severity == "block" for f in file_findings
    )
    return StartupVerificationResult(
        passed=not has_blocking,
        env_findings=env_findings,
        file_findings=file_findings,
    )


__all__ = (
    "EnvFinding",
    "FORBIDDEN_ENV_VARS",
    "StartupVerificationResult",
    "verify_airgap_startup",
    "verify_env",
    "verify_job_dir",
)


# Silence import-time "unused" warnings for symbols exported via __all__.
_ = Iterable

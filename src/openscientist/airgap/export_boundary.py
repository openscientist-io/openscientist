"""Air-gap export boundary — path exclusion + filesystem secret scan.

Implements RFC §11 and §12.2's last line of defense before any per-job
artifact leaves the box. Two complementary jobs:

1. **Path exclusion.** Even with :class:`AirgapCodexAgent` relocating
   ``CODEX_HOME`` outside ``job_dir`` (so ``config.toml`` and ``auth.json``
   shouldn't land in the export tree at all), a Codex CLI behavior change
   or operator misconfig could put them back. The exclusion filter is the
   belt to the agent's suspenders: anything under ``.codex/`` or matching
   the well-known secret-file patterns (``auth.json``, ``*.pem``, ``*.key``,
   SSH key shapes) is removed from the intended-export set regardless of
   what the upstream code thinks.

2. **Filesystem secret scan.** The agent's report and the artifact manifest
   are *designed* exfiltration channels (operator-released to peers), but
   they could still carry an inadvertent secret — an API key the LLM
   echoed back from a prompt, a JWT pasted into an error message, etc.
   :func:`scan_for_secrets` walks the export-bound files with a fixed
   ruleset of well-known secret shapes and returns findings; an export
   with any ``block``-severity finding is refused (:func:`evaluate_export`).

The :class:`ExportDecision` returned by :func:`evaluate_export` is what
:mod:`airgap.attestation` records into the per-job JSON; the artifact-ZIP
builder calls :func:`evaluate_export` and refuses the bundle if
``allowed is False``.

What's intentionally **not** in this module
-------------------------------------------

* **PDF text extraction.** Findings in the PDF report would require
  pdfplumber/pypdf — a real dependency the OS server doesn't carry today.
  Markdown source is scanned, which catches the same content (PDFs are
  rendered from it); this can change when full-text PDF scanning becomes
  a hard requirement. Tracked as a v2 refinement.
* **Deep DLP.** Entropy heuristics, contextual checks, ML classifiers.
  The ruleset is intentionally conservative: well-known shapes only,
  high-precision, low-false-positive. Stricter scanning is operator policy
  and would go in a separate ``airgap.dlp`` module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


Severity = Literal["block", "warn"]


# ----------------------------------------------------------------- secret rules


@dataclass(frozen=True)
class SecretRule:
    """One rule for the filesystem secret scanner.

    ``pattern`` is compiled once at module load; the scan walks files
    line-by-line and applies every rule's pattern. ``severity="block"``
    rules cause :func:`evaluate_export` to refuse the bundle; ``"warn"``
    rules surface in the report but don't block.
    """

    name: str
    pattern: re.Pattern[str]
    severity: Severity
    description: str = ""


def _compile(rules: list[tuple[str, str, Severity, str]]) -> list[SecretRule]:
    """Build the rule list from terse tuples (so the source stays scannable)."""
    return [
        SecretRule(
            name=name,
            pattern=re.compile(pat),
            severity=sev,
            description=desc,
        )
        for name, pat, sev, desc in rules
    ]


# High-precision shapes only. False positives in this scan block exports,
# so the rule bar is: a hit can only mean the named thing, not something
# that happens to look like it.
DEFAULT_SECRET_RULES: list[SecretRule] = _compile(
    [
        (
            "anthropic-api-key",
            r"sk-ant-(?:api03-)?[A-Za-z0-9_\-]{40,}",
            "block",
            "Anthropic API key (sk-ant-…).",
        ),
        (
            "openai-api-key-modern",
            r"sk-proj-[A-Za-z0-9_\-]{40,}",
            "block",
            "OpenAI project-scoped API key (sk-proj-…).",
        ),
        (
            "openai-api-key-classic",
            # Classic OpenAI keys are 'sk-' + 48 base62 chars. Lower bound is
            # 40 to tolerate near-future variations; tighter than the broad
            # 'sk-' match that would false-positive on script names.
            r"sk-[A-Za-z0-9]{40,}",
            "block",
            "OpenAI classic API key (sk-…).",
        ),
        (
            "github-pat",
            r"gh[pousr]_[A-Za-z0-9]{36}",
            "block",
            "GitHub personal-access / installation token.",
        ),
        (
            "aws-access-key-id",
            r"AKIA[0-9A-Z]{16}",
            "block",
            "AWS Access Key ID.",
        ),
        (
            "jwt",
            r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",
            "block",
            "JSON Web Token (eyJ…eyJ…).",
        ),
        (
            "pem-private-key",
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            "block",
            "PEM-encoded private key.",
        ),
        (
            "codex-auth-tokens-block",
            # The shape Codex CLI writes into auth.json — a top-level
            # 'tokens' JSON object containing access/refresh tokens.
            r'"tokens"\s*:\s*\{[^}]*"access_token"',
            "block",
            "Codex auth.json 'tokens' object.",
        ),
        (
            "azure-openai-deployment-key",
            # Azure OpenAI keys are 32 hex chars — high false-positive
            # surface (any hash, UUID-without-dashes), so 'warn' not block.
            r"\b[0-9a-f]{32}\b",
            "warn",
            "32-char hex (possibly an Azure OpenAI key; also commonly a hash).",
        ),
        (
            "bearer-token-generic",
            r"[Bb]earer\s+[A-Za-z0-9_\-\.=]{30,}",
            "warn",
            "HTTP 'Authorization: Bearer …' header value.",
        ),
    ]
)


# ----------------------------------------------------------------- exclusions


# Patterns evaluated against paths relative to ``job_dir``. Anything that
# matches is dropped from the intended-export set before scanning.
DEFAULT_EXCLUDED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"(^|/)\.codex(/|$)",  # Codex home (config.toml + auth.json)
        r"(^|/)auth\.json$",
        r"\.pem$",
        r"\.key$",
        r"(^|/)id_rsa(\.pub)?$",
        r"(^|/)id_ed25519(\.pub)?$",
        r"(^|/)\.ssh(/|$)",
        r"(^|/)\.aws(/|$)",
        r"(^|/)\.kube(/|$)",
    )
)


# ----------------------------------------------------------------- scanning


_MAX_SCAN_BYTES = 10 * 1024 * 1024  # 10 MB per file
_BINARY_PROBE_BYTES = 4 * 1024


@dataclass
class SecretFinding:
    """A single rule match in a single file."""

    rule_name: str
    severity: Severity
    path: Path
    line: int
    context: str  # ±40 chars around the match, with the match REDACTED
    description: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule_name,
            "severity": self.severity,
            "path": str(self.path),
            "line": self.line,
            "context": self.context,
            "description": self.description,
        }


def _looks_binary(data: bytes) -> bool:
    """Treat a file as binary if its first probe-window contains a null byte.

    This is a fast and conservative heuristic; it misses obscure text
    encodings (UTF-16 LE has null bytes between ASCII), but in practice
    the OS job dir is UTF-8 text + a small number of binary outputs (PDF,
    PNG) and the heuristic identifies them correctly.
    """
    return b"\x00" in data


def _redacted_context(line: str, match: re.Match[str], window: int = 40) -> str:
    """Return ±``window`` chars around ``match``, with the match itself redacted.

    Without redaction, ``SecretFinding.context`` would re-leak the very
    secret the scan just found — into the attestation record, into the
    operator's review UI, into logs.
    """
    start = max(0, match.start() - window)
    end = min(len(line), match.end() + window)
    redacted = line[start : match.start()] + "<REDACTED>" + line[match.end() : end]
    return redacted.rstrip("\n")


def scan_file_for_secrets(
    path: Path,
    rules: list[SecretRule] | None = None,
) -> list[SecretFinding]:
    """Scan one file against the rule list.

    Returns the empty list on missing/unreadable/binary files (not an
    exception — the export pipeline must keep going on the other files).
    """
    rules = rules if rules is not None else DEFAULT_SECRET_RULES
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0 or size > _MAX_SCAN_BYTES:
        return []
    try:
        with path.open("rb") as fp:
            head = fp.read(_BINARY_PROBE_BYTES)
        if _looks_binary(head):
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("export_boundary: skipping unreadable file %s: %s", path, exc)
        return []

    findings: list[SecretFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule in rules:
            for match in rule.pattern.finditer(line):
                findings.append(
                    SecretFinding(
                        rule_name=rule.name,
                        severity=rule.severity,
                        path=path,
                        line=lineno,
                        context=_redacted_context(line, match),
                        description=rule.description,
                    )
                )
    return findings


def scan_for_secrets(
    paths: Iterable[Path],
    rules: list[SecretRule] | None = None,
) -> list[SecretFinding]:
    """Scan a collection of files. Directories are recursed into."""
    rules = rules if rules is not None else DEFAULT_SECRET_RULES
    findings: list[SecretFinding] = []
    for path in paths:
        if path.is_dir():
            # rglob over the directory; the iterdir is sorted for
            # deterministic finding order (attestation diffs).
            for sub in sorted(path.rglob("*")):
                if sub.is_file():
                    findings.extend(scan_file_for_secrets(sub, rules))
        elif path.is_file():
            findings.extend(scan_file_for_secrets(path, rules))
    return findings


# ----------------------------------------------------------------- exclusion


def is_excluded(
    rel_path: str,
    patterns: tuple[re.Pattern[str], ...] = DEFAULT_EXCLUDED_PATTERNS,
) -> bool:
    """Return True if a relative path matches any exclusion pattern.

    ``rel_path`` is the path relative to ``job_dir``, with forward slashes
    (this is the form ZIP-builders use internally regardless of host OS).
    """
    return any(p.search(rel_path) for p in patterns)


def filter_paths_for_export(
    job_dir: Path,
    intended: Iterable[Path],
    patterns: tuple[re.Pattern[str], ...] = DEFAULT_EXCLUDED_PATTERNS,
) -> tuple[list[Path], list[Path]]:
    """Split ``intended`` into ``(allowed, excluded)`` by the exclusion rules.

    Each path is resolved relative to ``job_dir`` first (so an absolute
    path inside ``job_dir`` is normalized; a path outside ``job_dir`` is
    kept as absolute and matched against the patterns as-is — that surface
    is rare but the exclusion still applies).
    """
    allowed: list[Path] = []
    excluded: list[Path] = []
    for path in intended:
        try:
            rel = path.resolve().relative_to(job_dir.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        if is_excluded(rel, patterns):
            excluded.append(path)
        else:
            allowed.append(path)
    return allowed, excluded


# ----------------------------------------------------------------- top-level


@dataclass
class ExportDecision:
    """The combined output of the export boundary gate.

    The artifact-ZIP builder reads ``allowed``: True → proceed with the
    paths in ``allowed_paths``; False → refuse the bundle and surface
    ``blocking_findings`` for operator review.
    """

    allowed: bool
    allowed_paths: list[Path] = field(default_factory=list)
    excluded_paths: list[Path] = field(default_factory=list)
    findings: list[SecretFinding] = field(default_factory=list)

    @property
    def blocking_findings(self) -> list[SecretFinding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def warning_findings(self) -> list[SecretFinding]:
        return [f for f in self.findings if f.severity == "warn"]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "allowed_paths": [str(p) for p in self.allowed_paths],
            "excluded_paths": [str(p) for p in self.excluded_paths],
            "findings": [f.as_dict() for f in self.findings],
            "blocking_count": len(self.blocking_findings),
            "warning_count": len(self.warning_findings),
        }


def evaluate_export(
    job_dir: Path,
    intended: Iterable[Path] | None = None,
    rules: list[SecretRule] | None = None,
    exclusion_patterns: tuple[re.Pattern[str], ...] = DEFAULT_EXCLUDED_PATTERNS,
) -> ExportDecision:
    """Run the full export-boundary gate.

    Args:
        job_dir: Job directory; used as the base for path resolution and
            (when ``intended`` is None) as the default scan root.
        intended: The set of paths the upstream ZIP-builder wants to
            include. If None, the whole ``job_dir`` is walked.
        rules: Override the secret ruleset (mainly for tests).
        exclusion_patterns: Override the path-exclusion ruleset (mainly
            for tests).

    Returns:
        :class:`ExportDecision` whose ``allowed`` is False iff any
        ``block``-severity finding was discovered in the *allowed* (post-
        exclusion) files.
    """
    if intended is None:
        intended = [p for p in job_dir.rglob("*") if p.is_file()]

    allowed_paths, excluded_paths = filter_paths_for_export(job_dir, intended, exclusion_patterns)
    findings = scan_for_secrets(allowed_paths, rules)
    has_blocking = any(f.severity == "block" for f in findings)
    return ExportDecision(
        allowed=not has_blocking,
        allowed_paths=allowed_paths,
        excluded_paths=excluded_paths,
        findings=findings,
    )

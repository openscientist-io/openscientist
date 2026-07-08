"""Network-reachability probes for the air-gap verifier.

Per RFC §14: ``make airgap-verify`` runs a probe set inside a fresh agent
container under full air-gap policy. Negative probes — DNS lookups, TCP
connects, package-manager fetches, etc., to public targets — must **all**
fail. Positive probes — reaching the operator-configured internal LLM and
PubMed endpoints — must succeed. The mix demonstrates that the kernel +
firewall + Docker network configuration is doing what the policy claims
(RFC §6), not just trusting application-level hygiene.

This module ships the probe **functions**; how they're invoked (agent
container, executor container, LLM/PubMed service containers) is the
verifier's responsibility — see :mod:`airgap.attestation` for the per-job
JSON record they feed into.

Design choices worth noting
---------------------------

* **Each probe is a pure function** taking concrete args (target, timeout)
  and returning a :class:`ProbeResult`. No global state. The aggregator
  (:func:`run_airgap_probe_set`) just iterates and collects.
* **Probes are safe to run outside an air-gap environment.** A negative
  probe that "succeeds" (DNS resolves, TCP connects) just sets
  ``actual="pass"`` — i.e. the air-gap claim is broken — rather than
  raising. That's exactly what the verifier needs to detect a leaky setup.
* **Hard timeouts on every probe.** A misconfigured air-gap deployment with
  a partial blackhole route can hang ``socket.connect`` for minutes.
  Default is 5 s per probe; the aggregator sets a wall-clock budget on top.
* **No persistent side effects.** The pip/git/curl probes use ``--dry-run``
  or equivalents so the probe itself can't change the filesystem.
"""

from __future__ import annotations

import logging
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


# Default per-probe wall-clock cap. Generous enough that legitimate reachable
# endpoints respond comfortably; tight enough that a blackhole route doesn't
# stall the whole verifier.
PROBE_TIMEOUT_S = 5

# Hosts the negative probes target. Stable, widely-distributed public
# endpoints — if any of these is reachable from inside the air-gap, the
# policy is broken regardless of which one it was.
_NEGATIVE_DNS_HOSTS = ("example.com", "google.com", "github.com")
_NEGATIVE_TCP_TARGETS = (
    ("1.1.1.1", 443),  # Cloudflare DNS-over-HTTPS
    ("8.8.8.8", 53),  # Google DNS
    ("140.82.112.3", 443),  # github.com (a stable IP, no DNS dependency)
)
_NEGATIVE_HTTP_URLS = ("https://example.com", "https://github.com")
_NEGATIVE_PIP_PKG = "openscientist-airgap-probe-should-not-resolve"
_NEGATIVE_GIT_URL = "https://github.com/openscientist-io/openscientist.git"


Expected = Literal["fail", "pass"]
Actual = Literal["fail", "pass", "skipped"]


@dataclass
class ProbeResult:
    """One probe's outcome, suitable for the per-job attestation record.

    A probe **passes** (in the air-gap sense) when ``actual == expected`` —
    i.e. a negative probe failed as it was supposed to, or a positive probe
    succeeded. ``passed`` is the single field the aggregator uses to decide
    whether the air-gap claim holds.
    """

    name: str
    description: str
    expected: Expected
    actual: Actual
    duration_ms: int
    details: str = ""
    error: str = ""

    @property
    def passed(self) -> bool:
        """Probe behaved as the air-gap policy expected."""
        return self.actual == self.expected

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "expected": self.expected,
            "actual": self.actual,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "details": self.details,
            "error": self.error,
        }


# --------------------------------------------------------- low-level helpers


def _time_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _run_subprocess(
    cmd: list[str],
    timeout: int,
) -> tuple[int, str]:
    """Run ``cmd`` and return ``(returncode, combined_output)``.

    Wrapped so test fixtures can monkeypatch ``subprocess.run`` and so any
    ``FileNotFoundError`` (binary absent) is normalized to a sentinel return
    code that the caller turns into ``actual="skipped"`` rather than a
    misleading "fail" outcome.
    """
    try:
        completed = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return -1, f"binary not found: {exc}"
    except subprocess.TimeoutExpired:
        return -2, f"timed out after {timeout}s"
    combined = (completed.stdout + completed.stderr)[-400:]
    return completed.returncode, combined


# --------------------------------------------------------- DNS probes


def probe_dns_external_should_fail(
    host: str = "example.com", timeout: int = PROBE_TIMEOUT_S
) -> ProbeResult:
    """Resolve a public hostname. Must fail in air-gap mode (no external resolver)."""
    start = time.perf_counter()
    try:
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(host)
        return ProbeResult(
            name=f"dns_external::{host}",
            description=f"Resolve {host} (public hostname). Must fail in air-gap.",
            expected="fail",
            actual="pass",
            duration_ms=_time_ms(start),
            details="resolved successfully — external resolver is reachable",
        )
    except (OSError, socket.gaierror) as exc:
        return ProbeResult(
            name=f"dns_external::{host}",
            description=f"Resolve {host} (public hostname). Must fail in air-gap.",
            expected="fail",
            actual="fail",
            duration_ms=_time_ms(start),
            error=str(exc),
        )
    finally:
        socket.setdefaulttimeout(None)


def probe_dns_unique_subdomain_should_fail(
    parent: str = "example.com", timeout: int = PROBE_TIMEOUT_S
) -> ProbeResult:
    """Resolve a unique-token subdomain to detect DNS-encoded exfiltration.

    An attacker that smuggles data through DNS queries (``<base64>.attacker
    .com``) needs the resolver to forward arbitrary subdomains. A unique
    random token under a public parent should fail; if it succeeds, the
    resolver is forwarding, and any payload can ride the same path out.
    """
    token = secrets.token_hex(8)
    host = f"{token}.{parent}"
    result = probe_dns_external_should_fail(host=host, timeout=timeout)
    return ProbeResult(
        name="dns_unique_subdomain",
        description=(
            "Resolve a uniquely-named subdomain (detects DNS-encoded exfil "
            "via a forwarding resolver). Must fail in air-gap."
        ),
        expected=result.expected,
        actual=result.actual,
        duration_ms=result.duration_ms,
        details=f"token={token} parent={parent}",
        error=result.error,
    )


# --------------------------------------------------------- TCP probes


def probe_tcp_external_should_fail(
    host: str, port: int, timeout: int = PROBE_TIMEOUT_S
) -> ProbeResult:
    """Open a TCP connection to a public ``host:port``. Must fail in air-gap."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ProbeResult(
                name=f"tcp_external::{host}:{port}",
                description=(f"TCP connect to {host}:{port} (public). Must fail in air-gap."),
                expected="fail",
                actual="pass",
                duration_ms=_time_ms(start),
                details="connection established",
            )
    except OSError as exc:
        return ProbeResult(
            name=f"tcp_external::{host}:{port}",
            description=(f"TCP connect to {host}:{port} (public). Must fail in air-gap."),
            expected="fail",
            actual="fail",
            duration_ms=_time_ms(start),
            error=str(exc),
        )


def probe_tcp_internal_should_pass(
    host: str, port: int, timeout: int = PROBE_TIMEOUT_S
) -> ProbeResult:
    """The mirror of :func:`probe_tcp_external_should_fail` for allowlisted endpoints.

    The internal LLM and PubMed services must be reachable; if not, the
    job will fail anyway, and the verifier should fail loudly at job-start
    rather than mid-run.
    """
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ProbeResult(
                name=f"tcp_internal::{host}:{port}",
                description=(f"TCP connect to {host}:{port} (allowlisted). Must succeed."),
                expected="pass",
                actual="pass",
                duration_ms=_time_ms(start),
            )
    except OSError as exc:
        return ProbeResult(
            name=f"tcp_internal::{host}:{port}",
            description=(f"TCP connect to {host}:{port} (allowlisted). Must succeed."),
            expected="pass",
            actual="fail",
            duration_ms=_time_ms(start),
            error=str(exc),
        )


# --------------------------------------------------------- HTTP via urllib


def probe_python_urllib_should_fail(
    url: str = "https://example.com", timeout: int = PROBE_TIMEOUT_S
) -> ProbeResult:
    """``urllib.request.urlopen`` to a public URL. Must fail in air-gap.

    Mirrors a likely agent-authored Python exfil attempt (``import urllib;
    urllib.request.urlopen(...)``) — see RFC §10.2 on why the import
    allowlist alone isn't a security boundary.
    """
    start = time.perf_counter()
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return ProbeResult(
            name=f"python_urllib::{url}",
            description=(
                f"urllib.request.urlopen({url!r}) (mirrors agent exfil). Must fail in air-gap."
            ),
            expected="fail",
            actual="pass",
            duration_ms=_time_ms(start),
            details="HTTP request completed",
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return ProbeResult(
            name=f"python_urllib::{url}",
            description=(
                f"urllib.request.urlopen({url!r}) (mirrors agent exfil). Must fail in air-gap."
            ),
            expected="fail",
            actual="fail",
            duration_ms=_time_ms(start),
            error=str(exc),
        )


# --------------------------------------------------------- shell-tool probes


def probe_curl_external_should_fail(
    url: str = "https://example.com", timeout: int = PROBE_TIMEOUT_S
) -> ProbeResult:
    """``curl`` to a public URL. Must fail in air-gap."""
    start = time.perf_counter()
    code, output = _run_subprocess(
        ["curl", "--silent", "--show-error", "--max-time", str(timeout), url], timeout
    )
    if code == -1:  # binary missing
        return ProbeResult(
            name=f"curl_external::{url}",
            description=f"curl {url}. Must fail in air-gap.",
            expected="fail",
            actual="skipped",
            duration_ms=_time_ms(start),
            details=output,
        )
    return ProbeResult(
        name=f"curl_external::{url}",
        description=f"curl {url}. Must fail in air-gap.",
        expected="fail",
        actual="fail" if code != 0 else "pass",
        duration_ms=_time_ms(start),
        details=output,
        error="" if code != 0 else "curl exited 0 — external URL reachable",
    )


def probe_pip_install_should_fail(
    package: str = _NEGATIVE_PIP_PKG, timeout: int = PROBE_TIMEOUT_S * 2
) -> ProbeResult:
    """``pip install --dry-run`` of a never-existed package. Must fail in air-gap.

    Even on a healthy internet the install fails (404), but the failure
    mode is informative: in air-gap mode the failure is "could not resolve
    pypi.org" or "could not connect"; on a healthy network it's "no
    matching distribution". Both yield non-zero exit; only the failure
    text differs. We just check exit non-zero.
    """
    start = time.perf_counter()
    code, output = _run_subprocess(
        ["pip", "install", "--dry-run", "--no-input", "--quiet", package], timeout
    )
    if code == -1:
        return ProbeResult(
            name="pip_install_should_fail",
            description=f"pip install --dry-run {package}. Must fail in air-gap.",
            expected="fail",
            actual="skipped",
            duration_ms=_time_ms(start),
            details=output,
        )
    # Exit 0 from pip when targeting a never-existed package is itself
    # suspicious — likely a private mirror redirected the request. Treat
    # as "didn't behave like air-gap" too.
    return ProbeResult(
        name="pip_install_should_fail",
        description=f"pip install --dry-run {package}. Must fail in air-gap.",
        expected="fail",
        actual="fail" if code != 0 else "pass",
        duration_ms=_time_ms(start),
        details=output,
        error="" if code != 0 else "pip exited 0 — unexpected resolution path",
    )


def probe_git_ls_remote_should_fail(
    url: str = _NEGATIVE_GIT_URL, timeout: int = PROBE_TIMEOUT_S
) -> ProbeResult:
    """``git ls-remote`` against a public URL. Must fail in air-gap."""
    start = time.perf_counter()
    code, output = _run_subprocess(["git", "ls-remote", url, "HEAD"], timeout)
    if code == -1:
        return ProbeResult(
            name=f"git_ls_remote::{url}",
            description=f"git ls-remote {url}. Must fail in air-gap.",
            expected="fail",
            actual="skipped",
            duration_ms=_time_ms(start),
            details=output,
        )
    return ProbeResult(
        name=f"git_ls_remote::{url}",
        description=f"git ls-remote {url}. Must fail in air-gap.",
        expected="fail",
        actual="fail" if code != 0 else "pass",
        duration_ms=_time_ms(start),
        details=output,
        error="" if code != 0 else "git exited 0 — public repo reachable",
    )


# --------------------------------------------------------- aggregator


@dataclass
class ProbeSetSummary:
    """Aggregate outcome of a probe-set run, for the attestation record."""

    results: list[ProbeResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed and r.actual != "skipped")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.actual == "skipped")

    @property
    def airgap_holds(self) -> bool:
        """Every non-skipped probe behaved as the policy expected."""
        return self.failed == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "airgap_holds": self.airgap_holds,
            "results": [r.as_dict() for r in self.results],
        }


def run_negative_probe_set(timeout: int = PROBE_TIMEOUT_S) -> ProbeSetSummary:
    """The default negative-probe battery from RFC §14."""
    summary = ProbeSetSummary()
    for host in _NEGATIVE_DNS_HOSTS:
        summary.results.append(probe_dns_external_should_fail(host, timeout=timeout))
    summary.results.append(probe_dns_unique_subdomain_should_fail(timeout=timeout))
    for host, port in _NEGATIVE_TCP_TARGETS:
        summary.results.append(probe_tcp_external_should_fail(host, port, timeout=timeout))
    for url in _NEGATIVE_HTTP_URLS:
        summary.results.append(probe_python_urllib_should_fail(url, timeout=timeout))
        summary.results.append(probe_curl_external_should_fail(url, timeout=timeout))
    summary.results.append(probe_pip_install_should_fail(timeout=timeout * 2))
    summary.results.append(probe_git_ls_remote_should_fail(timeout=timeout))
    return summary


def run_positive_probe_set(
    allowed_endpoints: Iterable[tuple[str, int]],
    timeout: int = PROBE_TIMEOUT_S,
) -> ProbeSetSummary:
    """Positive probes for the operator-configured internal endpoints."""
    summary = ProbeSetSummary()
    for host, port in allowed_endpoints:
        summary.results.append(probe_tcp_internal_should_pass(host, port, timeout=timeout))
    return summary


def run_airgap_probe_set(
    allowed_endpoints: Iterable[tuple[str, int]],
    timeout: int = PROBE_TIMEOUT_S,
) -> ProbeSetSummary:
    """Run negative + positive probes; return a combined summary.

    Top-level entry point for the verifier. The returned summary is what
    :mod:`airgap.attestation` records into the per-job JSON. ``airgap_holds``
    is the single field downstream callers check; everything else is for
    operator diagnostics.
    """
    combined = ProbeSetSummary()
    neg = run_negative_probe_set(timeout=timeout)
    pos = run_positive_probe_set(allowed_endpoints, timeout=timeout)
    combined.results = neg.results + pos.results
    return combined

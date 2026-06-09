"""Per-job air-gap attestation record (RFC §14).

Where the other ``airgap/`` modules each enforce a single policy slice, this
module is the **integration point**: it takes their outputs plus the
system-level evidence the orchestrator collects (Docker network inspect,
``ip route``, firewall rules, image digests) and assembles a single signed
JSON record that is stored alongside the job artifacts.

The record is the auditable proof that the job ran under the air-gap policy:
an operator (or a CI ``make airgap-verify`` gate) re-verifies it by
recomputing the HMAC over the canonical-JSON encoding and checking the
nested sub-decisions (every probe passed, no blocking findings, the egress
registry resolved to the allowlist). A tampered field invalidates the
signature; a sub-decision that says "blocked" propagates to
:attr:`AttestationRecord.all_gates_passed`.

Why HMAC and not a public-key signature
---------------------------------------

Air-gap deployments don't have an external CA path by definition, and a
per-deployment signing key derived from the master secret is the simplest
shape that delivers tamper detection. The verifier needs the same key, so
this is **not** non-repudiable — the operator who signs it could also forge
it. That's fine for the threat model (the operator is trusted; §4) and
keeps the dependency surface to ``stdlib`` only. A v2 with X.509 + a
deployment CA is a follow-up if cross-org sharing of records becomes a
requirement.

What's in scope here
--------------------

* The :class:`AttestationRecord` dataclass — the canonical JSON shape.
* :func:`canonical_json` — sorted-keys, no-whitespace serialization (the
  signing input).
* :func:`sign` / :func:`verify` — HMAC-SHA256 with constant-time compare.
* :func:`build_attestation` — assembler that consumes the other modules'
  ``as_dict()`` outputs.
* :func:`derive_job_attestation_key` — convenience for deriving the
  per-job HMAC key from a master secret (the orchestrator's job).

What's not in scope
-------------------

* Collecting the **system-level evidence** (Docker network inspect, ``ip
  route`` inside the agent container, ``nft list ruleset``, ``docker
  version``). Those need root or Docker access and would make this module
  un-unit-testable; they're orchestrator concerns. The orchestrator passes
  the collected dicts to :func:`build_attestation` and we record them
  verbatim.
* X.509 or any non-stdlib crypto. HMAC only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


SCHEMA_VERSION = "1"
# Tag baked into the HMAC input so a key reused across signing contexts
# (e.g. the auth.storage_secret derivation in settings.py) can't be tricked
# into signing an attestation.
_HMAC_TAG = b"openscientist.airgap.attestation.v1"


# ----------------------------------------------------------------- dataclass


@dataclass
class AttestationRecord:
    """The canonical air-gap attestation record for one job.

    Every field is JSON-serializable. The orchestrator fills it in across
    the job lifecycle; :func:`build_attestation` is the high-level assembler.
    """

    # Identity
    job_id: str
    timestamp: str  # ISO 8601, always UTC ('Z' suffix)
    schema_version: str = SCHEMA_VERSION
    # Optional expiry timestamp (ISO 8601, UTC). When set, :func:`verify`
    # treats a current-time-past-``expires_at`` record as invalid even if
    # the HMAC is correct. PR #195 raises OPENSCIENTIST_AGENT_TIMEOUT to
    # 48 h (gpt-oss-120b runs are slow), so an attestation signed at
    # job-start has a long window where it remains technically valid;
    # the operator may want a freshness bound for downstream re-checks.
    expires_at: str = ""

    # Policy state
    airgap_mode: bool = False
    active_provider_id: str = ""

    # Egress registry — what the egress_registry validated for this provider
    # (the (host, port) set + the operator's allowlist + the pass/fail).
    egress_registry_result: dict[str, Any] = field(default_factory=dict)

    # System-level evidence (orchestrator-collected; recorded verbatim).
    docker_engine_version: str = ""
    docker_network_inspect: dict[str, Any] = field(default_factory=dict)
    ip_routes_v4: list[str] = field(default_factory=list)
    ip_routes_v6: list[str] = field(default_factory=list)
    firewall_rules: list[str] = field(default_factory=list)
    resolver_config: str = ""
    image_digests: dict[str, str] = field(default_factory=dict)
    codex_cli_digest: str = ""
    # Build-time provenance for the Codex CLI binary baked into the agent
    # image. Per RFC §8.1, the image is built once with full network access
    # on a non-airgap build host with the fork commit-pinned; this dict
    # records the supply-chain inputs so a verifier can detect drift
    # between deployments. Typical keys (operator-populated, e.g. via
    # build-args written to /etc/codex-provenance.json that the orchestrator
    # reads at startup):
    #   - "fork_commit"     — git commit hash of the open-codex fork
    #   - "rustc_version"   — output of `rustc --version`
    #   - "cargo_lock_hash" — SHA256 of /codex/codex-rs/Cargo.lock
    #   - "build_host_id"   — operator-defined build host identifier
    # Empty dict in this PR-1 lands; populated when the operator wires the
    # Dockerfile to emit a provenance manifest.
    codex_cli_provenance: dict[str, str] = field(default_factory=dict)

    # Policy enforcement outputs (from the other airgap/ modules' as_dict()).
    startup_verification: dict[str, Any] = field(default_factory=dict)
    export_decision: dict[str, Any] = field(default_factory=dict)
    probe_summary: dict[str, Any] = field(default_factory=dict)

    # Free-form notes the orchestrator wants to surface in the audit trail.
    notes: list[str] = field(default_factory=list)

    @property
    def all_gates_passed(self) -> bool:
        """Combined verdict across every sub-decision.

        The contract for downstream callers (CI ``airgap-verify``, the
        orchestrator) is: True iff *every* policy slice reports it held.
        Missing sub-decisions count as **not passing** — better to refuse
        than to declare an unattested run "passed" by accident.
        """
        gates = (
            self.startup_verification.get("passed"),
            self.export_decision.get("allowed"),
            self.probe_summary.get("airgap_holds"),
            self.egress_registry_result.get("passed"),
        )
        return all(g is True for g in gates)

    def as_dict(self) -> dict[str, Any]:
        # Manually serialize so the field order in the result is stable
        # (the dataclass declares the order we want). dataclasses.asdict()
        # would also work but copies more than we need.
        return {
            "job_id": self.job_id,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
            "expires_at": self.expires_at,
            "airgap_mode": self.airgap_mode,
            "active_provider_id": self.active_provider_id,
            "egress_registry_result": self.egress_registry_result,
            "docker_engine_version": self.docker_engine_version,
            "docker_network_inspect": self.docker_network_inspect,
            "ip_routes_v4": self.ip_routes_v4,
            "ip_routes_v6": self.ip_routes_v6,
            "firewall_rules": self.firewall_rules,
            "resolver_config": self.resolver_config,
            "image_digests": self.image_digests,
            "codex_cli_digest": self.codex_cli_digest,
            "codex_cli_provenance": self.codex_cli_provenance,
            "startup_verification": self.startup_verification,
            "export_decision": self.export_decision,
            "probe_summary": self.probe_summary,
            "notes": self.notes,
            "all_gates_passed": self.all_gates_passed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AttestationRecord":
        """Inverse of :meth:`as_dict` for verifier-side reconstruction.

        Unknown keys (newer schema, e.g. a v2 record loaded by a v1 reader)
        are silently dropped — the verifier still re-signs against the
        canonical form, so an unknown-field tamper still trips the HMAC.
        """
        known = {
            "job_id",
            "timestamp",
            "schema_version",
            "expires_at",
            "airgap_mode",
            "active_provider_id",
            "egress_registry_result",
            "docker_engine_version",
            "docker_network_inspect",
            "ip_routes_v4",
            "ip_routes_v6",
            "firewall_rules",
            "resolver_config",
            "image_digests",
            "codex_cli_digest",
            "codex_cli_provenance",
            "startup_verification",
            "export_decision",
            "probe_summary",
            "notes",
        }
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


# ----------------------------------------------------------------- signing


@dataclass
class SignedAttestation:
    """An :class:`AttestationRecord` plus its HMAC signature + key id.

    The on-disk shape is::

        {
            "record": { ... AttestationRecord.as_dict() ... },
            "signature": "<hex HMAC-SHA256 over canonical_json(record)>",
            "key_id": "<operator-chosen identifier for the signing key>",
            "algorithm": "HMAC-SHA256"
        }
    """

    record: AttestationRecord
    signature: str  # hex
    key_id: str
    algorithm: str = "HMAC-SHA256"

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.as_dict(),
            "signature": self.signature,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
        }

    def to_json(self) -> str:
        """Pretty-print for on-disk storage (humans + CI both read this)."""
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def canonical_json(record: AttestationRecord) -> str:
    """Deterministic serialization used as the HMAC input.

    Sorted keys, no whitespace, no trailing newline. The signature is
    computed over the **record** alone, not the all_gates_passed
    convenience field; ``all_gates_passed`` is derived from the other
    sub-decisions and including it would make the signature redundant.
    """
    d = record.as_dict()
    d.pop("all_gates_passed", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def sign(record: AttestationRecord, key: bytes, key_id: str) -> SignedAttestation:
    """HMAC-SHA256-sign the canonical JSON of ``record``."""
    payload = canonical_json(record).encode("utf-8")
    mac = hmac.new(key, _HMAC_TAG + payload, hashlib.sha256).hexdigest()
    return SignedAttestation(record=record, signature=mac, key_id=key_id)


def verify(
    signed: SignedAttestation,
    key: bytes,
    *,
    now: str | None = None,
) -> bool:
    """Re-verify a :class:`SignedAttestation` against ``key``.

    Uses :func:`hmac.compare_digest` (constant-time) so a side-channel can't
    leak the expected HMAC byte-by-byte. Wrong-key, wrong-algorithm,
    tampered-record, and (when ``record.expires_at`` is set) past-expiry
    records all return False.

    Args:
        signed: The :class:`SignedAttestation` to verify.
        key: HMAC key to verify against (usually
            :func:`derive_job_attestation_key`'s output).
        now: ISO 8601 UTC timestamp to compare against ``expires_at``.
            Defaults to the current wall-clock UTC. Inject for tests.
    """
    if signed.algorithm != "HMAC-SHA256":
        return False
    expected = sign(signed.record, key, signed.key_id).signature
    try:
        if not hmac.compare_digest(expected, signed.signature):
            return False
    except (TypeError, ValueError):
        return False
    # Freshness check — only when expires_at is set (otherwise the record
    # is open-ended, which is fine for the typical orchestrator workflow
    # that re-signs on every state transition).
    expires_at = signed.record.expires_at
    if expires_at:
        current = now or _utc_timestamp()
        if current > expires_at:
            return False
    return True


def load_signed(data: str | dict[str, Any]) -> SignedAttestation:
    """Reconstruct a :class:`SignedAttestation` from JSON text or a dict.

    The orchestrator writes attestations to disk via
    :meth:`SignedAttestation.to_json`; the verifier reads them back through
    this. Unknown record fields in the on-disk payload are silently dropped
    by :meth:`AttestationRecord.from_dict`, but the **signature** is still
    over the original bytes — so a tampered file still fails verification.
    """
    payload: dict[str, Any] = json.loads(data) if isinstance(data, str) else data
    record = AttestationRecord.from_dict(payload["record"])
    return SignedAttestation(
        record=record,
        signature=payload["signature"],
        key_id=payload["key_id"],
        algorithm=payload.get("algorithm", "HMAC-SHA256"),
    )


# ----------------------------------------------------------------- key derivation


def derive_job_attestation_key(master_secret: bytes, job_id: str) -> bytes:
    """Derive the per-job HMAC key from the master secret.

    Mirrors the pattern in ``settings.derive_secrets`` (HMAC-SHA256 over a
    domain tag). The orchestrator typically calls this with
    ``settings.secret_key.encode()`` and the active job id; tests can use
    any bytes.
    """
    domain = b"airgap.attestation.job_key:" + job_id.encode("utf-8")
    return hmac.new(master_secret, domain, hashlib.sha256).digest()


# ----------------------------------------------------------------- assembler


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_attestation(
    *,
    job_id: str,
    airgap_mode: bool,
    active_provider_id: str,
    egress_registry_result: dict[str, Any] | None = None,
    startup_verification: dict[str, Any] | None = None,
    export_decision: dict[str, Any] | None = None,
    probe_summary: dict[str, Any] | None = None,
    docker_engine_version: str = "",
    docker_network_inspect: dict[str, Any] | None = None,
    ip_routes_v4: Iterable[str] | None = None,
    ip_routes_v6: Iterable[str] | None = None,
    firewall_rules: Iterable[str] | None = None,
    resolver_config: str = "",
    image_digests: dict[str, str] | None = None,
    codex_cli_digest: str = "",
    codex_cli_provenance: dict[str, str] | None = None,
    notes: Iterable[str] | None = None,
    timestamp: str | None = None,
    expires_at: str = "",
) -> AttestationRecord:
    """Assemble an :class:`AttestationRecord` from the other modules' outputs.

    Every argument is optional and defaults to an empty value of the
    appropriate type — the orchestrator fills in what it has and the
    :attr:`AttestationRecord.all_gates_passed` property uses ``in``-style
    lookups (``.get("passed")``), so an unfilled section reads as
    "didn't pass" rather than crashing.

    The orchestrator's flow is roughly::

        record = build_attestation(
            job_id=job.id,
            airgap_mode=settings.airgap.enabled,
            active_provider_id=settings.provider.provider_id,
            egress_registry_result=egress_check.as_dict(),
            startup_verification=verifier_result.as_dict(),
            export_decision=export_decision.as_dict(),
            probe_summary=probes_summary.as_dict(),
            ... system evidence the orchestrator collected ...
        )
        key = derive_job_attestation_key(settings.secret_key.encode(), job.id)
        signed = sign(record, key, key_id=f"job:{job.id}")
        (job_dir / "attestation.json").write_text(signed.to_json())
    """
    return AttestationRecord(
        job_id=job_id,
        timestamp=timestamp or _utc_timestamp(),
        schema_version=SCHEMA_VERSION,
        expires_at=expires_at,
        airgap_mode=airgap_mode,
        active_provider_id=active_provider_id,
        egress_registry_result=egress_registry_result or {},
        docker_engine_version=docker_engine_version,
        docker_network_inspect=docker_network_inspect or {},
        ip_routes_v4=list(ip_routes_v4) if ip_routes_v4 else [],
        ip_routes_v6=list(ip_routes_v6) if ip_routes_v6 else [],
        firewall_rules=list(firewall_rules) if firewall_rules else [],
        resolver_config=resolver_config,
        image_digests=image_digests or {},
        codex_cli_digest=codex_cli_digest,
        codex_cli_provenance=codex_cli_provenance or {},
        startup_verification=startup_verification or {},
        export_decision=export_decision or {},
        probe_summary=probe_summary or {},
        notes=list(notes) if notes else [],
    )


__all__ = (
    "SCHEMA_VERSION",
    "AttestationRecord",
    "SignedAttestation",
    "build_attestation",
    "canonical_json",
    "derive_job_attestation_key",
    "load_signed",
    "sign",
    "verify",
)

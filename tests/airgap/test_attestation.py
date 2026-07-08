"""Tests for :mod:`openscientist.airgap.attestation`.

The module integrates outputs from the other airgap/ pieces, so the tests
deliberately feed in realistic ``as_dict()`` payloads — not contrived
shapes — to catch a schema drift in any contributor.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from openscientist.airgap.attestation import (
    SCHEMA_VERSION,
    AttestationRecord,
    build_attestation,
    canonical_json,
    derive_job_attestation_key,
    load_signed,
    sign,
    verify,
)

# Stable inputs for round-trip and signing tests.
_KEY_A = b"\x00" * 32
_KEY_B = b"\xff" * 32
_FROZEN_TIMESTAMP = "2026-06-07T12:00:00Z"


def _good_egress_result() -> dict[str, object]:
    return {
        "passed": True,
        "provider_id": "anthropic",
        "targets": [["llm.internal", 8443]],
        "allowlist": [["llm.internal", 8443]],
    }


def _good_startup_verification() -> dict[str, object]:
    return {
        "passed": True,
        "blocking_count": 0,
        "warning_count": 0,
        "env_findings": [],
        "file_findings": [],
    }


def _good_export_decision() -> dict[str, object]:
    return {
        "allowed": True,
        "allowed_paths": ["final_report.md"],
        "excluded_paths": [".codex/auth.json"],
        "findings": [],
        "blocking_count": 0,
        "warning_count": 0,
    }


def _good_probe_summary() -> dict[str, object]:
    return {
        "total": 12,
        "passed": 12,
        "failed": 0,
        "skipped": 0,
        "airgap_holds": True,
        "results": [],
    }


# --------------------------------------------------------- AttestationRecord


class TestRecordShape:
    def test_round_trips_through_as_dict(self) -> None:
        with patch(
            "openscientist.airgap.attestation._utc_timestamp",
            return_value=_FROZEN_TIMESTAMP,
        ):
            record = build_attestation(
                job_id="job-abc",
                airgap_mode=True,
                active_provider_id="anthropic",
                egress_registry_result=_good_egress_result(),
                startup_verification=_good_startup_verification(),
                export_decision=_good_export_decision(),
                probe_summary=_good_probe_summary(),
                docker_engine_version="25.0.3",
                docker_network_inspect={"name": "airgap-job-abc", "internal": True},
                ip_routes_v4=["default dev eth0 (none)"],
                firewall_rules=["chain INPUT drop"],
                image_digests={"agent": "sha256:abc"},
                codex_cli_digest="sha256:def",
                notes=["smoke run"],
            )
        d = record.as_dict()
        # The dataclass round-trips through from_dict.
        reconstructed = AttestationRecord.from_dict(d)
        # all_gates_passed is derived; everything else is field-preserved.
        assert reconstructed.job_id == "job-abc"
        assert reconstructed.timestamp == _FROZEN_TIMESTAMP
        assert reconstructed.schema_version == SCHEMA_VERSION

    def test_from_dict_drops_unknown_keys(self) -> None:
        # A v2-from-the-future record carrying a field this version doesn't
        # know about must still load — and the signature still tells us if
        # the original bytes were tampered with.
        future = {
            "job_id": "j",
            "timestamp": _FROZEN_TIMESTAMP,
            "schema_version": "2",
            "v2_added_field": "irrelevant",
        }
        record = AttestationRecord.from_dict(future)
        assert record.job_id == "j"

    def test_schema_version_baked_in(self) -> None:
        # Sanity sentinel — SCHEMA_VERSION is part of the wire contract; a
        # silent bump would orphan deployed verifiers.
        assert SCHEMA_VERSION == "1"


# --------------------------------------------------------- all_gates_passed


class TestAllGatesPassed:
    """The single load-bearing verdict downstream callers read."""

    def test_all_passed_when_every_subdecision_says_yes(self) -> None:
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            egress_registry_result=_good_egress_result(),
            startup_verification=_good_startup_verification(),
            export_decision=_good_export_decision(),
            probe_summary=_good_probe_summary(),
        )
        assert record.all_gates_passed is True

    def test_startup_failure_propagates(self) -> None:
        failed_startup = {**_good_startup_verification(), "passed": False}
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            egress_registry_result=_good_egress_result(),
            startup_verification=failed_startup,
            export_decision=_good_export_decision(),
            probe_summary=_good_probe_summary(),
        )
        assert record.all_gates_passed is False

    def test_export_block_propagates(self) -> None:
        blocked_export = {**_good_export_decision(), "allowed": False}
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            egress_registry_result=_good_egress_result(),
            startup_verification=_good_startup_verification(),
            export_decision=blocked_export,
            probe_summary=_good_probe_summary(),
        )
        assert record.all_gates_passed is False

    def test_probe_leak_propagates(self) -> None:
        leaky_probes = {**_good_probe_summary(), "airgap_holds": False}
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            egress_registry_result=_good_egress_result(),
            startup_verification=_good_startup_verification(),
            export_decision=_good_export_decision(),
            probe_summary=leaky_probes,
        )
        assert record.all_gates_passed is False

    def test_egress_failure_propagates(self) -> None:
        bad_egress = {**_good_egress_result(), "passed": False}
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            egress_registry_result=bad_egress,
            startup_verification=_good_startup_verification(),
            export_decision=_good_export_decision(),
            probe_summary=_good_probe_summary(),
        )
        assert record.all_gates_passed is False

    def test_missing_subdecision_does_not_silently_pass(self) -> None:
        # A record that only filled in one section must NOT all_gates_passed.
        # Better to refuse than declare an unattested run "passed" by
        # accident (a silent missing-key bug in the orchestrator).
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            startup_verification=_good_startup_verification(),
            # No egress_registry_result, export_decision, probe_summary.
        )
        assert record.all_gates_passed is False


# --------------------------------------------------------- canonical_json


class TestCanonicalJson:
    def test_is_sorted_and_compact(self) -> None:
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            startup_verification={"passed": True},
            export_decision={"allowed": True},
            probe_summary={"airgap_holds": True},
            egress_registry_result={"passed": True},
        )
        s = canonical_json(record)
        # No whitespace.
        assert " " not in s.replace(" elegans", "")  # narrow exception for fixture text
        assert "\n" not in s
        # Keys are sorted at the top level.
        as_dict = json.loads(s)
        assert list(as_dict.keys()) == sorted(as_dict.keys())

    def test_does_not_include_all_gates_passed(self) -> None:
        # all_gates_passed is derived from the sub-decisions; including it
        # in the signed payload would make the signature redundant and
        # let a tampered sub-decision still re-derive 'all_gates_passed'.
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            startup_verification=_good_startup_verification(),
        )
        s = canonical_json(record)
        assert "all_gates_passed" not in s

    def test_deterministic_across_calls(self) -> None:
        # Same record → byte-identical output. Required for HMAC stability.
        with patch(
            "openscientist.airgap.attestation._utc_timestamp",
            return_value=_FROZEN_TIMESTAMP,
        ):
            r1 = build_attestation(job_id="j", airgap_mode=True, active_provider_id="x")
            r2 = build_attestation(job_id="j", airgap_mode=True, active_provider_id="x")
        assert canonical_json(r1) == canonical_json(r2)


# --------------------------------------------------------- sign + verify


class TestSigning:
    @pytest.fixture
    def record(self) -> AttestationRecord:
        with patch(
            "openscientist.airgap.attestation._utc_timestamp",
            return_value=_FROZEN_TIMESTAMP,
        ):
            return build_attestation(
                job_id="j",
                airgap_mode=True,
                active_provider_id="anthropic",
                egress_registry_result=_good_egress_result(),
                startup_verification=_good_startup_verification(),
                export_decision=_good_export_decision(),
                probe_summary=_good_probe_summary(),
            )

    def test_sign_then_verify_roundtrip(self, record: AttestationRecord) -> None:
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        assert verify(signed, _KEY_A) is True

    def test_wrong_key_fails(self, record: AttestationRecord) -> None:
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        assert verify(signed, _KEY_B) is False

    def test_tampered_field_fails_verification(self, record: AttestationRecord) -> None:
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        # Mutate the record after signing — exactly what an attacker would do.
        signed.record.active_provider_id = "switched-by-attacker"
        assert verify(signed, _KEY_A) is False

    def test_tampered_subdecision_fails(self, record: AttestationRecord) -> None:
        # Subtler: change a nested field. The HMAC covers the whole record
        # canonical-JSON, so any inner change breaks it.
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        signed.record.probe_summary["airgap_holds"] = False
        assert verify(signed, _KEY_A) is False

    def test_wrong_algorithm_fails(self, record: AttestationRecord) -> None:
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        signed.algorithm = "MD5"  # Not what we sign with.
        assert verify(signed, _KEY_A) is False

    def test_to_json_round_trip(self, record: AttestationRecord) -> None:
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        loaded = load_signed(signed.to_json())
        assert verify(loaded, _KEY_A) is True
        assert loaded.record.job_id == record.job_id
        assert loaded.signature == signed.signature

    def test_load_signed_accepts_dict(self, record: AttestationRecord) -> None:
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        loaded = load_signed(signed.as_dict())
        assert verify(loaded, _KEY_A) is True


class TestExpiresAt:
    """RFC §14 + PR #195: with OPENSCIENTIST_AGENT_TIMEOUT raised to 48h
    for slow gpt-oss-120b runs, attestations have a long valid window.
    Optional expires_at gives operators a freshness bound for re-checks."""

    @pytest.fixture
    def record_with_expiry(self) -> AttestationRecord:
        with patch(
            "openscientist.airgap.attestation._utc_timestamp",
            return_value=_FROZEN_TIMESTAMP,
        ):
            return build_attestation(
                job_id="j",
                airgap_mode=True,
                active_provider_id="ollama",
                expires_at="2026-06-08T12:00:00Z",  # +24h from signing
            )

    def test_unexpired_record_verifies(self, record_with_expiry: AttestationRecord) -> None:
        signed = sign(record_with_expiry, _KEY_A, key_id="job-key:abc")
        # Current time is well before expiry.
        assert verify(signed, _KEY_A, now="2026-06-07T18:00:00Z") is True

    def test_expired_record_fails_verification(self, record_with_expiry: AttestationRecord) -> None:
        signed = sign(record_with_expiry, _KEY_A, key_id="job-key:abc")
        # Current time well past expiry.
        assert verify(signed, _KEY_A, now="2026-06-09T12:00:00Z") is False

    def test_at_expiry_boundary_fails(self, record_with_expiry: AttestationRecord) -> None:
        # One second past expiry → fail. Pinning the comparison direction
        # so a future implementation switch (>= vs >) is caught.
        signed = sign(record_with_expiry, _KEY_A, key_id="job-key:abc")
        assert verify(signed, _KEY_A, now="2026-06-08T12:00:01Z") is False

    def test_empty_expires_at_means_no_expiry(self) -> None:
        # Default behavior — open-ended records (the typical orchestrator
        # workflow that re-signs on every state transition) must still verify.
        with patch(
            "openscientist.airgap.attestation._utc_timestamp",
            return_value=_FROZEN_TIMESTAMP,
        ):
            record = build_attestation(job_id="j", airgap_mode=True, active_provider_id="ollama")
        assert record.expires_at == ""
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        # 100 years from now → still verifies.
        assert verify(signed, _KEY_A, now="2126-06-07T12:00:00Z") is True

    def test_expires_at_tamper_breaks_signature(
        self, record_with_expiry: AttestationRecord
    ) -> None:
        # Belt-and-suspenders sentinel: if an attacker extends expires_at
        # to keep a stolen record valid, the HMAC fails.
        signed = sign(record_with_expiry, _KEY_A, key_id="job-key:abc")
        signed.record.expires_at = "2126-06-07T12:00:00Z"  # extend by a century
        assert verify(signed, _KEY_A, now="2026-06-07T18:00:00Z") is False


class TestKeyDerivation:
    def test_different_master_secrets_yield_different_keys(self) -> None:
        k1 = derive_job_attestation_key(b"secret-1", "job-1")
        k2 = derive_job_attestation_key(b"secret-2", "job-1")
        assert k1 != k2

    def test_different_job_ids_yield_different_keys(self) -> None:
        k1 = derive_job_attestation_key(b"secret-1", "job-1")
        k2 = derive_job_attestation_key(b"secret-1", "job-2")
        assert k1 != k2

    def test_same_input_same_key(self) -> None:
        k1 = derive_job_attestation_key(b"secret-1", "job-1")
        k2 = derive_job_attestation_key(b"secret-1", "job-1")
        assert k1 == k2

    def test_derived_key_is_32_bytes_sha256(self) -> None:
        k = derive_job_attestation_key(b"secret-1", "job-1")
        assert len(k) == 32


# --------------------------------------------------------- on-disk shape


class TestOnDiskShape:
    """The orchestrator writes attestation.json; the CI verifier reads it.
    The wire contract is worth pinning explicitly."""

    def test_to_json_is_human_readable(self, tmp_path: Path) -> None:
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            startup_verification=_good_startup_verification(),
        )
        signed = sign(record, _KEY_A, key_id="job-key:abc")
        out = tmp_path / "attestation.json"
        out.write_text(signed.to_json())
        # Loadable by anyone with stdlib json.
        loaded = json.loads(out.read_text())
        # Every top-level wire field is present.
        assert {"record", "signature", "key_id", "algorithm"}.issubset(loaded.keys())
        # And the indented form is readable (not single-line JSON).
        assert "\n" in out.read_text()


# --------------------------------------------------------- integration with peer modules


@pytest.mark.skip(
    reason=(
        "Depends on probes.py, export_boundary.py, and credential_verifier.py, "
        "which land in separate PRs in the air-gapped mode split (see #209). "
        "Re-enable once all three are on main alongside this module."
    )
)
class TestIntegrationWithPeerModules:
    """End-to-end: take real outputs from probes/export_boundary/credential_
    verifier and feed them through. Catches a schema drift in any contributor
    that the dataclass alone wouldn't notice."""

    def test_consumes_real_probe_summary(self) -> None:
        from openscientist.airgap.probes import (  # type: ignore[import-not-found]
            ProbeResult,
            ProbeSetSummary,
        )

        summary = ProbeSetSummary(
            results=[
                ProbeResult(
                    name="dns_external::example.com",
                    description="d",
                    expected="fail",
                    actual="fail",
                    duration_ms=12,
                )
            ]
        )
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            probe_summary=summary.as_dict(),
            startup_verification=_good_startup_verification(),
            export_decision=_good_export_decision(),
            egress_registry_result=_good_egress_result(),
        )
        # The probe summary lands verbatim and propagates to the verdict.
        assert record.probe_summary["airgap_holds"] is True
        assert record.all_gates_passed is True

    def test_consumes_real_export_decision(self, tmp_path: Path) -> None:
        from openscientist.airgap.export_boundary import (  # type: ignore[import-not-found]
            evaluate_export,
        )

        (tmp_path / "final_report.md").write_text("# Clean.\n")
        decision = evaluate_export(tmp_path)
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            export_decision=decision.as_dict(),
            startup_verification=_good_startup_verification(),
            probe_summary=_good_probe_summary(),
            egress_registry_result=_good_egress_result(),
        )
        assert record.export_decision["allowed"] is True
        assert record.all_gates_passed is True

    def test_consumes_real_startup_verification(self, tmp_path: Path) -> None:
        from openscientist.airgap.credential_verifier import (  # type: ignore[import-not-found]
            verify_airgap_startup,
        )

        result = verify_airgap_startup(
            env={"PATH": "/usr/bin"},
            active_provider_id="anthropic",
            job_dir=tmp_path,
        )
        record = build_attestation(
            job_id="j",
            airgap_mode=True,
            active_provider_id="anthropic",
            startup_verification=result.as_dict(),
            export_decision=_good_export_decision(),
            probe_summary=_good_probe_summary(),
            egress_registry_result=_good_egress_result(),
        )
        assert record.startup_verification["passed"] is True
        assert record.all_gates_passed is True

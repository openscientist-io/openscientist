"""Tests for :mod:`openscientist.airgap.export_boundary`.

Each test seeds a temporary job_dir with realistic file content and
exercises the two gate stages independently (exclusion + scan), plus the
combined ``evaluate_export`` entry point. The fixture data deliberately
mixes legitimate report content with secret-shaped strings so the
redaction-in-context behavior gets exercised too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openscientist.airgap.export_boundary import (
    DEFAULT_SECRET_RULES,
    ExportDecision,
    SecretFinding,
    SecretRule,
    evaluate_export,
    filter_paths_for_export,
    is_excluded,
    scan_file_for_secrets,
    scan_for_secrets,
)

# --------------------------------------------------------- DEFAULT_SECRET_RULES


class TestRulesetShape:
    """The ruleset is the load-bearing piece; smoke-test its structure."""

    def test_every_rule_has_a_compiled_pattern(self) -> None:
        for rule in DEFAULT_SECRET_RULES:
            assert hasattr(rule.pattern, "finditer")
            assert rule.severity in ("block", "warn")
            assert rule.description  # human-readable, not blank

    def test_well_known_shapes_present(self) -> None:
        # Adding/removing a rule from the default set is a security-
        # relevant change — assert the named ones still exist so a
        # well-intentioned rewrite doesn't silently drop coverage.
        names = {r.name for r in DEFAULT_SECRET_RULES}
        assert "anthropic-api-key" in names
        assert "openai-api-key-classic" in names
        assert "openai-api-key-modern" in names
        assert "github-pat" in names
        assert "aws-access-key-id" in names
        assert "jwt" in names
        assert "pem-private-key" in names
        assert "codex-auth-tokens-block" in names


# --------------------------------------------------------- scan_file_for_secrets


class TestScanFile:
    def test_anthropic_key_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "report.md"
        # 50 base62 chars — solidly past the lower bound.
        f.write_text(
            "Some text.\n"
            "Leaked: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKl\n"
            "More text.\n"
        )
        findings = scan_file_for_secrets(f)
        assert any(x.rule_name == "anthropic-api-key" for x in findings)
        # Line number is correct (line 2, 1-indexed).
        anthropic = next(x for x in findings if x.rule_name == "anthropic-api-key")
        assert anthropic.line == 2

    def test_match_is_redacted_in_context(self, tmp_path: Path) -> None:
        # Without redaction, the finding's context would re-leak the very
        # secret the scan just found into the attestation record.
        secret = "sk-ant-api03-" + "A" * 60
        f = tmp_path / "report.md"
        f.write_text(f"Before {secret} after.\n")
        findings = scan_file_for_secrets(f)
        assert len(findings) >= 1
        for finding in findings:
            assert secret not in finding.context, (
                f"context for {finding.rule_name} leaked the secret: {finding.context!r}"
            )
            assert "<REDACTED>" in finding.context

    def test_github_pat_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "log.txt"
        f.write_text("Auth: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n")
        findings = scan_file_for_secrets(f)
        assert any(x.rule_name == "github-pat" for x in findings)

    def test_aws_access_key_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "env.txt"
        f.write_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
        findings = scan_file_for_secrets(f)
        assert any(x.rule_name == "aws-access-key-id" for x in findings)

    def test_jwt_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "config.toml"
        # Real-shape JWT (header.payload.signature, all base64url).
        f.write_text('token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.signed_part_here"\n')
        findings = scan_file_for_secrets(f)
        assert any(x.rule_name == "jwt" for x in findings)

    def test_pem_private_key_detected(self, tmp_path: Path) -> None:
        f = tmp_path / "id_rsa"
        f.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n")
        findings = scan_file_for_secrets(f)
        assert any(x.rule_name == "pem-private-key" for x in findings)

    def test_codex_auth_tokens_block(self, tmp_path: Path) -> None:
        f = tmp_path / "auth.json"
        f.write_text('{"tokens": {"access_token": "abc.def.ghi"}}\n')
        findings = scan_file_for_secrets(f)
        block_findings = [x for x in findings if x.severity == "block"]
        assert any(x.rule_name == "codex-auth-tokens-block" for x in block_findings)

    def test_clean_file_yields_no_findings(self, tmp_path: Path) -> None:
        f = tmp_path / "report.md"
        f.write_text(
            "# Discovery report\n\n"
            "We analyzed the dataset and found three clusters of interest.\n"
            "The methodology is described in the methods section below.\n"
        )
        findings = scan_file_for_secrets(f)
        assert findings == []

    def test_binary_file_skipped(self, tmp_path: Path) -> None:
        # Heuristic: null byte in first 4 KB → treat as binary, skip.
        # Without skipping, a PNG/PDF would trigger a flood of false hits.
        f = tmp_path / "fig.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100 + b"sk-ant-fake\n")
        findings = scan_file_for_secrets(f)
        assert findings == []

    def test_too_large_file_skipped(self, tmp_path: Path) -> None:
        # The scanner has a 10 MB-per-file cap so a multi-GB transcript
        # doesn't stall the export gate. The cap is a UX choice; the file
        # should be excluded by upstream rules anyway in real deployments.
        f = tmp_path / "huge.txt"
        f.write_bytes(b"x" * (11 * 1024 * 1024))
        findings = scan_file_for_secrets(f)
        assert findings == []

    def test_empty_file_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert scan_file_for_secrets(f) == []

    def test_nonexistent_file_yields_empty(self, tmp_path: Path) -> None:
        # Missing files don't raise — the export pipeline must keep going.
        assert scan_file_for_secrets(tmp_path / "nope.md") == []

    def test_warn_severity_for_bearer_token(self, tmp_path: Path) -> None:
        # Bearer tokens are warn, not block — they're commonly seen in
        # paste'd error messages and shouldn't reflexively kill the export.
        f = tmp_path / "log.txt"
        f.write_text("Authorization: Bearer abc1234567890XYZabcdef1234567890abcdef==\n")
        findings = scan_file_for_secrets(f)
        warns = [x for x in findings if x.severity == "warn"]
        assert any(x.rule_name == "bearer-token-generic" for x in warns)


# --------------------------------------------------------- scan_for_secrets (multi)


class TestScanMulti:
    def test_directory_is_recursed(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "leaky.md").write_text("Leaked sk-ant-api03-" + "A" * 50 + "\n")
        (tmp_path / "clean.md").write_text("Clean content.\n")
        findings = scan_for_secrets([tmp_path])
        names = {f.rule_name for f in findings}
        assert "anthropic-api-key" in names

    def test_mixed_files_and_dirs(self, tmp_path: Path) -> None:
        d = tmp_path / "d"
        d.mkdir()
        (d / "a.txt").write_text("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
        f = tmp_path / "b.txt"
        f.write_text("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n")
        findings = scan_for_secrets([d, f])
        names = {fnd.rule_name for fnd in findings}
        assert "aws-access-key-id" in names
        assert "github-pat" in names

    def test_custom_ruleset_overrides_default(self, tmp_path: Path) -> None:
        # A test-only ruleset that only catches a marker string. The
        # default Anthropic key in this file should NOT fire.
        import re

        custom = [
            SecretRule(
                name="marker",
                pattern=re.compile(r"MAGIC_MARKER"),
                severity="block",
                description="test rule",
            )
        ]
        f = tmp_path / "report.md"
        f.write_text("sk-ant-api03-" + "A" * 50 + " MAGIC_MARKER text\n")
        findings = scan_for_secrets([f], rules=custom)
        assert len(findings) == 1
        assert findings[0].rule_name == "marker"


# --------------------------------------------------------- exclusion


class TestIsExcluded:
    @pytest.mark.parametrize(
        "rel_path,expected",
        [
            (".codex/config.toml", True),
            (".codex/auth.json", True),
            ("nested/.codex/config.toml", True),
            ("auth.json", True),
            ("subdir/auth.json", True),
            ("server.pem", True),
            ("server.key", True),
            ("id_rsa", True),
            (".ssh/id_ed25519", True),
            (".aws/credentials", True),
            # Legitimate exports — must not match.
            ("final_report.md", False),
            ("final_report.pdf", False),
            ("knowledge_state.json", False),  # auth.json substring, but exact match required
            ("provenance/iter1_transcript.json", False),
            ("AGENTS.md", False),
        ],
    )
    def test_default_exclusions(self, rel_path: str, expected: bool) -> None:
        assert is_excluded(rel_path) is expected


class TestFilterPathsForExport:
    def test_splits_into_allowed_and_excluded(self, tmp_path: Path) -> None:
        (tmp_path / ".codex").mkdir()
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.write_text("model = 'test'\n")
        report = tmp_path / "final_report.md"
        report.write_text("# Discovery\n")
        auth = tmp_path / ".codex" / "auth.json"
        auth.write_text('{"tokens": {}}\n')

        allowed, excluded = filter_paths_for_export(tmp_path, [codex_config, report, auth])
        assert report in allowed
        assert codex_config in excluded
        assert auth in excluded

    def test_absolute_paths_outside_job_dir(self, tmp_path: Path) -> None:
        # A path outside job_dir can't be made relative; the function
        # still applies the exclusion patterns against its as_posix().
        external = tmp_path / "outside.pem"
        external.write_text("-----BEGIN RSA PRIVATE KEY-----\n")
        other_job_dir = tmp_path / "job"
        other_job_dir.mkdir()
        _, excluded = filter_paths_for_export(other_job_dir, [external])
        assert external in excluded


# --------------------------------------------------------- evaluate_export


class TestEvaluateExport:
    """The combined gate: filter + scan, returning a single decision."""

    def test_clean_export_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "final_report.md").write_text("# Discovery\nSome findings.\n")
        decision = evaluate_export(tmp_path)
        assert decision.allowed is True
        assert decision.findings == []

    def test_codex_artifacts_excluded_not_scanned(self, tmp_path: Path) -> None:
        # The whole point of §11: a Codex auth.json that landed in job_dir
        # by mistake gets excluded *before* it's exported AND before it's
        # scanned — so even if its contents contain block-severity
        # patterns, the export still proceeds (with the file dropped).
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text('{"tokens": {"access_token": "secret"}}\n')
        (tmp_path / "final_report.md").write_text("# Clean report.\n")
        decision = evaluate_export(tmp_path)
        assert decision.allowed is True
        # The auth.json is excluded from export.
        auth = tmp_path / ".codex" / "auth.json"
        assert auth in decision.excluded_paths
        assert auth not in decision.allowed_paths
        # And no findings, because excluded files aren't scanned.
        assert decision.findings == []

    def test_secret_in_report_blocks_export(self, tmp_path: Path) -> None:
        # The clean half of the contract: a real secret in the *report*
        # (the operator-released channel) does block the export.
        (tmp_path / "final_report.md").write_text(
            "# Discovery\n\nFor reference: sk-ant-api03-" + "A" * 60 + "\n"
        )
        decision = evaluate_export(tmp_path)
        assert decision.allowed is False
        assert len(decision.blocking_findings) >= 1
        assert decision.blocking_findings[0].rule_name == "anthropic-api-key"

    def test_warn_only_findings_do_not_block(self, tmp_path: Path) -> None:
        # Bearer tokens are warn — surface in findings, but allowed=True.
        (tmp_path / "log.txt").write_text(
            "Server returned 401 Authorization: Bearer " + "X" * 50 + " — please update\n"
        )
        decision = evaluate_export(tmp_path)
        assert decision.allowed is True
        assert len(decision.warning_findings) >= 1
        assert decision.blocking_findings == []

    def test_intended_paths_override_walk(self, tmp_path: Path) -> None:
        # When intended is provided, only those paths are scanned —
        # files in job_dir that aren't intended for export are ignored.
        (tmp_path / "scratch.md").write_text("sk-ant-api03-" + "A" * 60 + "\n")
        (tmp_path / "final_report.md").write_text("# Clean.\n")
        decision = evaluate_export(tmp_path, intended=[tmp_path / "final_report.md"])
        assert decision.allowed is True
        assert decision.findings == []

    def test_decision_as_dict_contract(self, tmp_path: Path) -> None:
        (tmp_path / "final_report.md").write_text("Clean.\n")
        d = evaluate_export(tmp_path).as_dict()
        assert d["allowed"] is True
        assert d["blocking_count"] == 0
        assert "allowed_paths" in d
        assert "excluded_paths" in d
        assert "findings" in d


# --------------------------------------------------------- ExportDecision


class TestExportDecisionConvenience:
    def test_splits_block_vs_warn(self) -> None:
        block_finding = SecretFinding(
            rule_name="r1", severity="block", path=Path("a"), line=1, context=""
        )
        warn_finding = SecretFinding(
            rule_name="r2", severity="warn", path=Path("b"), line=2, context=""
        )
        decision = ExportDecision(allowed=False, findings=[block_finding, warn_finding])
        assert decision.blocking_findings == [block_finding]
        assert decision.warning_findings == [warn_finding]

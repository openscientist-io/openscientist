"""Tests for :mod:`openscientist.airgap.credential_verifier`.

The verifier is the *begin* gate (export_boundary is the *end* gate), so
the tests focus on the things a startup-time scan needs to be loud about:
forbidden-by-name vars regardless of value, cross-provider credentials
that the env_allowlist should have stripped, and any secret-shaped content
sitting in the freshly-prepared job_dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openscientist.airgap.credential_verifier import (
    FORBIDDEN_ENV_VARS,
    EnvFinding,
    StartupVerificationResult,
    verify_airgap_startup,
    verify_env,
    verify_job_dir,
)

# Real-shaped fixture secrets — match the production patterns so the value
# scan actually fires. Defined once to avoid re-tuning across tests.
_REAL_ANTHROPIC_KEY = "sk-ant-api03-" + "A" * 60
_REAL_OPENAI_PROJ_KEY = "sk-proj-" + "B" * 60
_REAL_GITHUB_PAT = "ghp_" + "C" * 36
_REAL_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


# --------------------------------------------------------- verify_env (forbidden-by-name)


class TestForbiddenByName:
    """The first-stage check: certain var names must never be in the
    agent env in air-gap mode regardless of their value."""

    def test_forbidden_env_vars_set_is_locked(self) -> None:
        # Adding a new var to FORBIDDEN_ENV_VARS is a policy change worth
        # being explicit about. Pin the set so a rewrite doesn't widen or
        # narrow it accidentally.
        assert FORBIDDEN_ENV_VARS == frozenset({"OPENSCIENTIST_SECRET_KEY", "DATABASE_URL"})

    def test_master_secret_flagged(self) -> None:
        findings = verify_env(
            {"OPENSCIENTIST_SECRET_KEY": "abc"},
            active_provider_id="anthropic",
        )
        assert len(findings) == 1
        f = findings[0]
        assert f.rule_name == "forbidden-env-var"
        assert f.severity == "block"
        assert f.var_name == "OPENSCIENTIST_SECRET_KEY"
        # The value itself is redacted in the context (it could be the
        # master secret, after all).
        assert "abc" not in f.context

    def test_database_url_flagged_even_when_innocent_value(self) -> None:
        # If DATABASE_URL is present at all, it's a config-bypass signal.
        findings = verify_env(
            {"DATABASE_URL": "postgresql://reader@localhost/x"},
            active_provider_id="anthropic",
        )
        assert any(f.var_name == "DATABASE_URL" for f in findings)


# --------------------------------------------------------- verify_env (value scan)


class TestValueShapeScan:
    """The second-stage check: every non-active-provider var has its value
    scanned against the secret-shape rule set."""

    def test_inactive_provider_credential_flagged(self) -> None:
        # Active is Anthropic; an OpenAI key in env means the env_allowlist
        # filter let a cross-provider credential through.
        findings = verify_env(
            {"OPENAI_API_KEY": _REAL_OPENAI_PROJ_KEY},
            active_provider_id="anthropic",
        )
        assert len(findings) == 1
        assert findings[0].rule_name == "openai-api-key-modern"
        assert findings[0].severity == "block"

    def test_active_provider_creds_not_flagged(self) -> None:
        # Active is openai; its OWN key is allowed to be present (the
        # PROVIDER_ENV_VARS allowlist for "openai" includes OPENAI_API_KEY).
        findings = verify_env(
            {"OPENAI_API_KEY": _REAL_OPENAI_PROJ_KEY},
            active_provider_id="openai",
        )
        assert findings == [], (
            "Active provider's own credential must not be flagged — that's "
            "the entire point of the env_allowlist's per-provider exemption."
        )

    def test_active_provider_creds_not_flagged_anthropic(self) -> None:
        # Mirror check — Anthropic active, Anthropic key present, no findings.
        findings = verify_env(
            {
                "ANTHROPIC_API_KEY": _REAL_ANTHROPIC_KEY,
                "ANTHROPIC_BASE_URL": "https://llm.internal:8443",
            },
            active_provider_id="anthropic",
        )
        assert findings == []

    def test_secret_in_unexpected_var_flagged(self) -> None:
        # A custom-named var carrying a secret-shaped value: the var name
        # is unknown to the allowlist, but the shape rule catches the value.
        findings = verify_env(
            {"SOMEONES_PRIVATE_KEY": _REAL_GITHUB_PAT},
            active_provider_id="anthropic",
        )
        assert len(findings) == 1
        assert findings[0].rule_name == "github-pat"

    def test_one_finding_per_var(self) -> None:
        # A value that could match multiple rules contributes one finding
        # — keeps the report compact and avoids "this same secret matches
        # 4 rules" noise.
        # sk- + 50 base62 satisfies both classic-openai pattern and the
        # generic 'sk-' rule.
        findings = verify_env(
            {"BIZARRE_VAR": "sk-" + "X" * 50},
            active_provider_id="anthropic",
        )
        assert len(findings) == 1

    def test_block_severity_wins_over_warn_regardless_of_rule_order(self) -> None:
        # Codex Review-5: a caller-supplied custom rules list with WARN
        # ordered before BLOCK must NOT mask the BLOCK match. The scanner
        # picks the highest-severity match across all rules, not the first
        # match.
        import re as _re

        from openscientist.airgap.export_boundary import SecretRule

        # Both rules match any 'XXX' string; warn is listed first.
        custom_rules = [
            SecretRule(
                name="warn-rule",
                pattern=_re.compile(r"XXX"),
                severity="warn",
                description="warn match",
            ),
            SecretRule(
                name="block-rule",
                pattern=_re.compile(r"XXX"),
                severity="block",
                description="block match",
            ),
        ]
        findings = verify_env(
            {"SOME_VAR": "leaked-XXX-value"},
            active_provider_id="anthropic",
            rules=custom_rules,
        )
        assert len(findings) == 1
        assert findings[0].severity == "block", (
            "BLOCK match must win even when WARN is earlier in the rules "
            "list — first-match-wins would silently mask the BLOCK"
        )
        assert findings[0].rule_name == "block-rule"

    def test_value_redacted_in_finding_context(self) -> None:
        # Critical: the finding's context must NOT contain the matched
        # secret. Without this, the verifier itself becomes a leak channel.
        findings = verify_env(
            {"OPENAI_API_KEY": _REAL_OPENAI_PROJ_KEY},
            active_provider_id="anthropic",
        )
        assert findings
        assert _REAL_OPENAI_PROJ_KEY not in findings[0].context
        assert "<REDACTED>" in findings[0].context

    def test_empty_value_not_scanned(self) -> None:
        # Empty / None values are a no-op (some operators set placeholder
        # vars to ""; not a credential leak).
        findings = verify_env(
            {"OPENAI_API_KEY": ""},
            active_provider_id="anthropic",
        )
        assert findings == []

    def test_non_secret_values_pass(self) -> None:
        # Sanity: ordinary config values don't trigger anything.
        findings = verify_env(
            {
                "PATH": "/usr/bin:/bin",
                "OPENSCIENTIST_MODEL": "claude-opus-4-7",
                "OPENSCIENTIST_AIRGAP_LLM_ADDR": "10.0.0.5:8443",
            },
            active_provider_id="anthropic",
        )
        assert findings == []

    def test_unknown_provider_id_strips_all_creds(self) -> None:
        # An unknown active_provider_id has no PROVIDER_ENV_VARS entry → no
        # vars exempted → every credential in env is flagged. Defensive
        # default — if the orchestrator passes a typo'd provider id, we
        # don't accidentally exempt everything.
        findings = verify_env(
            {
                "ANTHROPIC_API_KEY": _REAL_ANTHROPIC_KEY,
                "AWS_ACCESS_KEY_ID": _REAL_AWS_KEY,
            },
            active_provider_id="not-a-real-provider",
        )
        assert {f.rule_name for f in findings} == {
            "anthropic-api-key",
            "aws-access-key-id",
        }


# --------------------------------------------------------- verify_job_dir


class TestVerifyJobDir:
    def test_empty_job_dir_passes(self, tmp_path: Path) -> None:
        findings = verify_job_dir(tmp_path)
        assert findings == []

    def test_secret_in_input_file_flagged(self, tmp_path: Path) -> None:
        # An input data file with a secret in it shouldn't be there at
        # job start.
        (tmp_path / "prompt.txt").write_text(
            f"My API key is {_REAL_ANTHROPIC_KEY}, please use it.\n"
        )
        findings = verify_job_dir(tmp_path)
        assert any(f.rule_name == "anthropic-api-key" for f in findings)
        # All findings are block (the begin gate is strict).
        assert all(
            f.severity == "block"
            for f in findings
            if f.rule_name != "azure-openai-deployment-key"
            and f.rule_name != "bearer-token-generic"
        )

    def test_recurses_into_subdirs(self, tmp_path: Path) -> None:
        d = tmp_path / "inputs"
        d.mkdir()
        (d / "config.json").write_text(f'{{"key": "{_REAL_OPENAI_PROJ_KEY}"}}\n')
        findings = verify_job_dir(tmp_path)
        assert any(f.rule_name == "openai-api-key-modern" for f in findings)

    def test_missing_job_dir_yields_empty(self, tmp_path: Path) -> None:
        # Missing dir = nothing to scan = no findings, no exception. The
        # orchestrator may call this before mkdir() in some flows.
        findings = verify_job_dir(tmp_path / "not-yet-created")
        assert findings == []


# --------------------------------------------------------- verify_airgap_startup (top-level)


class TestVerifyAirgapStartup:
    """The orchestrator's entrypoint: combined env + job_dir scan."""

    def test_clean_startup_passes(self, tmp_path: Path) -> None:
        result = verify_airgap_startup(
            env={
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": _REAL_ANTHROPIC_KEY,
                "OPENSCIENTIST_MODEL": "claude-opus-4-7",
            },
            active_provider_id="anthropic",
            job_dir=tmp_path,
        )
        assert result.passed is True
        assert result.blocking_count == 0

    def test_env_leak_blocks_startup(self, tmp_path: Path) -> None:
        # Anthropic active, but OpenAI key leaked through env_allowlist.
        result = verify_airgap_startup(
            env={"OPENAI_API_KEY": _REAL_OPENAI_PROJ_KEY},
            active_provider_id="anthropic",
            job_dir=tmp_path,
        )
        assert result.passed is False
        assert result.blocking_count >= 1
        assert any(f.rule_name == "openai-api-key-modern" for f in result.blocking_env)

    def test_job_dir_leak_blocks_startup(self, tmp_path: Path) -> None:
        # Env is clean but a previous-run scratch file is sitting in job_dir.
        (tmp_path / "scratch.json").write_text(f'{{"old_key": "{_REAL_AWS_KEY}"}}\n')
        result = verify_airgap_startup(
            env={"PATH": "/usr/bin"},
            active_provider_id="anthropic",
            job_dir=tmp_path,
        )
        assert result.passed is False
        assert any(f.rule_name == "aws-access-key-id" for f in result.blocking_files)

    def test_warning_only_does_not_block(self, tmp_path: Path) -> None:
        # 32-char hex is warn (could be Azure key or a hash); don't refuse.
        # Put it in a file so the file_findings path is exercised.
        (tmp_path / "manifest.json").write_text('{"hash": "0123456789abcdef0123456789abcdef"}\n')
        result = verify_airgap_startup(
            env={"PATH": "/usr/bin"},
            active_provider_id="anthropic",
            job_dir=tmp_path,
        )
        assert result.passed is True
        assert result.warning_count >= 1
        assert result.blocking_count == 0

    def test_as_dict_round_trip(self, tmp_path: Path) -> None:
        # The orchestrator embeds this in the attestation record; the dict
        # contract must be stable.
        result = verify_airgap_startup(
            env={"PATH": "/usr/bin"},
            active_provider_id="anthropic",
            job_dir=tmp_path,
        )
        d = result.as_dict()
        assert d["passed"] is True
        assert d["blocking_count"] == 0
        assert "env_findings" in d
        assert "file_findings" in d


# --------------------------------------------------------- StartupVerificationResult convenience


class TestResultConvenience:
    def test_blocking_count_sums_env_and_files(self) -> None:
        from openscientist.airgap.export_boundary import SecretFinding

        env_f = EnvFinding(
            var_name="X", rule_name="r1", severity="block", context="", description=""
        )
        file_f = SecretFinding(
            rule_name="r2",
            severity="block",
            path=Path("a"),
            line=1,
            context="",
        )
        result = StartupVerificationResult(
            passed=False, env_findings=[env_f], file_findings=[file_f]
        )
        assert result.blocking_count == 2

    def test_warn_findings_excluded_from_blocking(self) -> None:
        env_warn = EnvFinding(
            var_name="X", rule_name="r", severity="warn", context="", description=""
        )
        result = StartupVerificationResult(passed=True, env_findings=[env_warn], file_findings=[])
        assert result.blocking_count == 0
        assert result.warning_count == 1


# --------------------------------------------------------- parametrized: each supported provider


@pytest.mark.parametrize("provider_id", ["anthropic", "cborg", "openai", "azure-openai", "foundry"])
def test_each_supported_provider_skips_own_creds(provider_id: str) -> None:
    """For each provider env_allowlist supports, the verifier exempts that
    provider's own credential vars from the value scan.

    Regression sentinel: a refactor that loses the PROVIDER_ENV_VARS lookup
    would false-flag every active provider's own key — which is, ironically,
    the most common case in production.
    """
    from openscientist.airgap.env_allowlist import PROVIDER_ENV_VARS

    provider_vars = PROVIDER_ENV_VARS[provider_id]
    # Build an env with each of this provider's vars set to a secret-shaped value.
    env = {var: _REAL_ANTHROPIC_KEY for var in provider_vars}
    findings = verify_env(env, active_provider_id=provider_id)
    assert findings == [], (
        f"Provider {provider_id}'s own creds {provider_vars} should be exempted; "
        f"got findings {findings}"
    )

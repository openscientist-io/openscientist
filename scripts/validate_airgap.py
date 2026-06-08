#!/usr/bin/env python3
"""Tier-3 airgap validation — exercises every airgap dispatch path against
a real Ollama instance without needing the fork-built Codex CLI binary.

What this proves:
  - AirgapSettings constructs and the model_validator fires correctly
  - agent.factory.get_agent() returns AirgapCodexAgent for an Ollama provider
  - airgap.egress_registry.validate_provider_for_airgap() resolves the
    Ollama endpoint against the operator's allowlist
  - airgap.credential_verifier.verify_airgap_startup() runs and produces a
    clean StartupVerificationResult against a realistic env
  - AirgapCodexAgent._codex_home() relocates outside the job_dir
  - AirgapCodexAgent._mcp_env() strips inactive-provider creds
  - airgap.attestation.build_attestation/sign/verify roundtrips cleanly,
    eating real outputs from the modules above

What this does NOT prove:
  - The actual Codex CLI subprocess launches and talks to Ollama (needs the
    fork build; that's Tier 4)
  - The kernel/firewall guarantee actually drops external packets (needs
    Linux + nftables; that's Tier 5)
  - A real discovery loop completes (needs the full agent container)

If this script exits 0, our orchestrator-layer airgap integration with
PR #195 is sound; any breakage left is in the agent-container runtime and
gets surfaced by the actual container run, not by this script.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


def main() -> int:
    failures: list[str] = []

    # -------------------------------------------------------------- Ollama up?
    _section("Ollama daemon reachable")
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            body = r.read().decode()
        if "gpt-oss" not in body:
            failures.append("Ollama up but no gpt-oss model installed")
            _fail("no gpt-oss model installed; expected gpt-oss:20b or :120b")
        else:
            _ok("Ollama is up and serves gpt-oss")
    except (urllib.error.URLError, OSError) as exc:
        failures.append(f"Ollama not reachable: {exc}")
        _fail(f"Ollama not reachable on 127.0.0.1:11434: {exc}")
        return _summary(failures)

    # -------------------------------------------------------------- settings
    _section("AirgapSettings constructs + validates")
    # Build a minimal env that the validator accepts. Use the host Ollama
    # via host.docker.internal — but for THIS script we run outside a
    # container, so just use 127.0.0.1.
    test_env = {
        "OPENSCIENTIST_AIR_GAPPED": "true",
        "OPENSCIENTIST_AIRGAP_LLM_ADDR": "127.0.0.1:11434",
        "OPENSCIENTIST_AIRGAP_PUBMED_ADDR": "127.0.0.1:9000",
        # Use a writable tmpfs-substitute on macOS for the per-job CODEX_HOME.
        "OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT": "/tmp/airgap-codex-home",
        "OPENSCIENTIST_PROVIDER": "ollama",
        "OPENSCIENTIST_MODEL": "gpt-oss:120b",
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
        "OLLAMA_MODEL": "gpt-oss:120b",
        # Required-but-unrelated bits the rest of Settings needs.
        "OPENSCIENTIST_SECRET_KEY": "test-secret-not-real",
        "DATABASE_URL": "postgresql+asyncpg://test@localhost/test",
    }
    with patch.dict(os.environ, test_env, clear=False):
        from openscientist.airgap.env_allowlist import (
            BASE_AIRGAP_ENV,
            PROVIDER_ENV_VARS,
            filtered_agent_env,
        )
        from openscientist.settings import AirgapSettings, Settings, clear_settings_cache

        clear_settings_cache()
        try:
            s = Settings(_env_file=None)
            airgap = s.airgap
        except Exception as exc:
            failures.append(f"Settings construction failed: {exc}")
            _fail(f"settings: {exc}")
            return _summary(failures)
        _ok(f"airgap.enabled = {airgap.enabled}")
        _ok(f"airgap.llm_addr = {airgap.llm_addr}")
        _ok(f"airgap.pubmed_addr = {airgap.pubmed_addr}")
        _ok(f"airgap.docker_socket_path = {airgap.docker_socket_path}")
        _ok(f"airgap.codex_home_root = {airgap.codex_home_root}")
        assert airgap.enabled is True
        assert airgap.docker_socket_path != "/var/run/docker.sock"

        # -------------------------------------------------------- egress registry
        _section("egress_registry validates ollama against allowlist")
        from openscientist.airgap.egress_registry import (
            AirGapPolicyError,
            egress_targets_for,
            validate_provider_for_airgap,
        )

        targets = egress_targets_for("ollama", s)
        _ok(f"ollama targets: {targets}")
        assert targets == {("127.0.0.1", 11434)}, f"unexpected targets: {targets}"

        # Allowlist matches → passes
        ok_targets = validate_provider_for_airgap(
            "ollama", s, allowlist={("127.0.0.1", 11434), ("127.0.0.1", 9000)}
        )
        assert ok_targets == targets
        _ok("validate_provider_for_airgap passes when ollama endpoint is in allowlist")

        # Allowlist doesn't match → refuses
        try:
            validate_provider_for_airgap("ollama", s, allowlist={("10.0.0.99", 443)})
        except AirGapPolicyError as e:
            _ok(f"refused when ollama not in allowlist: {str(e)[:80]}…")
        else:
            failures.append("validate_provider_for_airgap should have refused")
            _fail("did not refuse a mismatched allowlist")

        # ----------------------------------------------- credential_verifier
        _section("credential_verifier scans the filtered env")
        from openscientist.airgap.credential_verifier import verify_airgap_startup

        # Build a realistic agent env: filter through env_allowlist, then verify.
        polluted = {
            **dict(os.environ),
            # Cross-provider noise that the env_allowlist should strip.
            "OPENAI_API_KEY": "sk-proj-" + "B" * 60,
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "ANTHROPIC_API_KEY": "sk-ant-api03-" + "A" * 60,
        }
        filtered = filtered_agent_env(polluted, "ollama")
        for cred in ("OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "ANTHROPIC_API_KEY"):
            assert cred not in filtered, f"env_allowlist failed to strip {cred}"
        _ok("env_allowlist strips OPENAI_API_KEY, AWS_ACCESS_KEY_ID, ANTHROPIC_API_KEY")
        assert "OLLAMA_BASE_URL" in filtered
        _ok("env_allowlist preserves OLLAMA_BASE_URL (active provider)")

        # Run the startup verifier on the filtered env against a fresh job_dir.
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            job_dir.mkdir()
            result = verify_airgap_startup(filtered, "ollama", job_dir)
        if result.passed:
            _ok("credential_verifier.verify_airgap_startup passed")
        else:
            failures.append(
                f"verify_airgap_startup failed: {result.blocking_count} blocking findings"
            )
            for f in result.blocking_env + result.blocking_files:
                _fail(f"  → {f.rule_name}: {getattr(f, 'var_name', getattr(f, 'path', '?'))}")

        # ----------------------------------------------- factory dispatch
        _section("agent.factory selects AirgapCodexAgent for ollama")
        from openscientist.agent.base import AgentConfig
        from openscientist.agent.factory import get_agent
        from openscientist.airgap.codex_agent import AirgapCodexAgent
        from openscientist.providers.ollama import OllamaProvider

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "test-job"
            job_dir.mkdir()
            config = AgentConfig(job_dir=job_dir, system_prompt="probe")
            agent = get_agent(config)
        assert isinstance(agent, AirgapCodexAgent), (
            f"factory returned {type(agent).__name__}, expected AirgapCodexAgent"
        )
        assert isinstance(agent.provider, OllamaProvider)
        _ok(f"get_agent → {type(agent).__name__} with provider {agent.provider.id}")

        # ----------------------------------------------- AirgapCodexAgent surfaces
        _section("AirgapCodexAgent's three overrides behave as advertised")
        # _codex_home is OUTSIDE the job_dir (this is the §11/§12.2 contract).
        codex_home = agent._codex_home()
        _ok(f"_codex_home() = {codex_home}")
        assert not codex_home.is_relative_to(agent._config.job_dir), (
            f"_codex_home() {codex_home} must NOT be inside job_dir "
            f"{agent._config.job_dir} (RFC §11/§12.2)"
        )
        _ok("_codex_home is outside job_dir (RFC §11/§12.2 — auth.json/config.toml "
            "kept out of export tree)")

        # _mcp_env strips inactive-provider creds.
        with patch.dict(os.environ, polluted, clear=False):
            mcp_env = agent._mcp_env()
        for cred in ("OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "ANTHROPIC_API_KEY"):
            assert cred not in mcp_env, f"_mcp_env didn't strip {cred}"
        assert "OLLAMA_BASE_URL" in mcp_env
        _ok("_mcp_env strips cross-provider creds, preserves ollama's own")
        assert mcp_env["OPENSCIENTIST_JOB_ID"] == agent._config.job_dir.name
        _ok("_mcp_env's per-job overlay is correct")

        # _ensure_auth is a no-op — no shutil.copy2 invoked.
        with patch("shutil.copy2") as mock_copy:
            agent._ensure_auth()
        assert not mock_copy.called, "_ensure_auth() must NOT copy from host"
        _ok("_ensure_auth is a no-op (host filesystem untouched)")

        # ----------------------------------------------- attestation roundtrip
        _section("attestation builds, signs, verifies against a realistic record")
        from openscientist.airgap.attestation import (
            build_attestation,
            derive_job_attestation_key,
            sign,
            verify,
        )

        record = build_attestation(
            job_id="validate-airgap-001",
            airgap_mode=True,
            active_provider_id="ollama",
            egress_registry_result={
                "passed": True,
                "provider_id": "ollama",
                "targets": [list(t) for t in targets],
                "allowlist": [["127.0.0.1", 11434]],
            },
            startup_verification=result.as_dict(),
            probe_summary={"total": 0, "passed": 0, "failed": 0, "airgap_holds": True},
            export_decision={
                "allowed": True,
                "allowed_paths": [],
                "excluded_paths": [],
                "allowed_findings": [],
                "excluded_findings": [],
                "blocking_count": 0,
                "warning_count": 0,
            },
            image_digests={"agent": "sha256:fake-for-tier-3"},
            notes=["Tier-3 validation script — no real container run"],
        )
        key = derive_job_attestation_key(s.secret_key.encode(), record.job_id)
        signed = sign(record, key, key_id=f"job:{record.job_id}")
        _ok(f"signed (HMAC head 12 hex chars): {signed.signature[:12]}…")
        assert verify(signed, key), "signed record didn't verify"
        _ok("verify() round-trips")
        # Tamper-detection sentinel
        signed.record.active_provider_id = "tampered"
        assert not verify(signed, key), "verify() must reject tampered record"
        _ok("verify() rejects a tampered record")
        assert record.all_gates_passed
        _ok(f"all_gates_passed = {record.all_gates_passed}")

    return _summary(failures)


def _summary(failures: list[str]) -> int:
    print()
    if not failures:
        print("┌─────────────────────────────────────────────────────────────┐")
        print("│  Tier-3 airgap validation: ALL CHECKS PASSED                │")
        print("│  Orchestrator-layer airgap integration with PR #195 sound.  │")
        print("└─────────────────────────────────────────────────────────────┘")
        return 0
    print("Tier-3 validation failed:")
    for f in failures:
        print(f"  • {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

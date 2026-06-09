"""Sentinel for the air-gap startup verifier wiring in
:func:`openscientist.orchestrator.discovery._enforce_airgap_startup_policy`.

Codex Review-6 GAP (fixed): the airgap startup verifier and probe set
existed only as test fixtures — no production lifecycle hook called
them, so a misconfigured airgap deployment would have let the agent
start with cross-provider creds leaking into the env or with secret
residue in the job_dir. The orchestrator's ``run_discovery_async`` now
calls ``_enforce_airgap_startup_policy`` before constructing the agent.

This test covers the helper directly because the full
``run_discovery_async`` path requires extensive runtime context (DB,
provider, etc.) that's out of scope for this gate-fires sentinel.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _settings(*, airgap_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(airgap=SimpleNamespace(enabled=airgap_enabled))


@pytest.mark.asyncio
async def test_non_airgap_skips_verifier_entirely(tmp_path: Path) -> None:
    from openscientist.orchestrator.discovery import _enforce_airgap_startup_policy

    with (
        patch(
            "openscientist.settings.get_settings",
            return_value=_settings(airgap_enabled=False),
        ),
        patch("openscientist.airgap.credential_verifier.verify_airgap_startup") as mock_verify,
    ):
        # Function must return without calling the verifier when airgap is off.
        await _enforce_airgap_startup_policy("job-42", tmp_path, "anthropic")
    mock_verify.assert_not_called()


@pytest.mark.asyncio
async def test_airgap_calls_verifier_and_proceeds_on_pass(tmp_path: Path) -> None:
    from openscientist.airgap.credential_verifier import StartupVerificationResult
    from openscientist.orchestrator.discovery import _enforce_airgap_startup_policy

    clean = StartupVerificationResult(passed=True)
    with (
        patch(
            "openscientist.settings.get_settings",
            return_value=_settings(airgap_enabled=True),
        ),
        patch(
            "openscientist.airgap.credential_verifier.verify_airgap_startup",
            return_value=clean,
        ) as mock_verify,
    ):
        await _enforce_airgap_startup_policy("job-42", tmp_path, "ollama")
    mock_verify.assert_called_once()
    # The active_provider_id is the third arg the function takes.
    assert mock_verify.call_args.kwargs.get("active_provider_id") == "ollama"


@pytest.mark.asyncio
async def test_airgap_raises_on_blocking_finding(tmp_path: Path) -> None:
    # The load-bearing test — a blocking finding refuses to start the agent.
    from openscientist.airgap.credential_verifier import (
        EnvFinding,
        StartupVerificationResult,
    )
    from openscientist.orchestrator.discovery import _enforce_airgap_startup_policy

    leak = StartupVerificationResult(
        passed=False,
        env_findings=[
            EnvFinding(
                var_name="OPENAI_API_KEY",
                rule_name="openai-api-key-modern",
                severity="block",
                context="<REDACTED>",
                description="leaked cross-provider cred",
            )
        ],
    )
    with (
        patch(
            "openscientist.settings.get_settings",
            return_value=_settings(airgap_enabled=True),
        ),
        patch(
            "openscientist.airgap.credential_verifier.verify_airgap_startup",
            return_value=leak,
        ),
    ):
        with pytest.raises(RuntimeError, match="refused job"):
            await _enforce_airgap_startup_policy("job-42", tmp_path, "anthropic")


@pytest.mark.asyncio
async def test_warning_findings_do_not_block(tmp_path: Path) -> None:
    # Warnings are surfaced (log line) but do not raise.
    from openscientist.airgap.credential_verifier import (
        EnvFinding,
        StartupVerificationResult,
    )
    from openscientist.orchestrator.discovery import _enforce_airgap_startup_policy

    warn_only = StartupVerificationResult(
        passed=True,
        env_findings=[
            EnvFinding(
                var_name="WEIRD_VAR",
                rule_name="bearer-token-generic",
                severity="warn",
                context="<REDACTED>",
                description="",
            )
        ],
    )
    with (
        patch(
            "openscientist.settings.get_settings",
            return_value=_settings(airgap_enabled=True),
        ),
        patch(
            "openscientist.airgap.credential_verifier.verify_airgap_startup",
            return_value=warn_only,
        ),
    ):
        # Should not raise.
        await _enforce_airgap_startup_policy("job-42", tmp_path, "ollama")

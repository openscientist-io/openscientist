"""Tests for docker/agent-entrypoint.py's airgap credential-verification gate.

Not part of the ``openscientist`` package -- it's a standalone deployment
artifact (the agent container's sole entry point), loaded here by file path
the same way ``docker/airgap-docker-proxy/validator/validator.py`` is loaded
in ``test_docker_proxy_validator.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[2] / "docker" / "agent-entrypoint.py"


def _load_entrypoint_module():
    spec = importlib.util.spec_from_file_location("agent_entrypoint", _ENTRYPOINT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


entrypoint = _load_entrypoint_module()


# --------------------------------------------------------- _airgap_mode_requested


class TestAirgapModeRequested:
    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, truthy: str) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", truthy)
        assert entrypoint._airgap_mode_requested() is True

    @pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off"])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", falsy)
        assert entrypoint._airgap_mode_requested() is False

    def test_unset_is_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENSCIENTIST_AIR_GAPPED", raising=False)
        assert entrypoint._airgap_mode_requested() is False


# --------------------------------------------------------- main() credential gate


@dataclass
class _FakeFinding:
    var_name: str = "OPENSCIENTIST_SECRET_KEY"
    rule_name: str = "test-rule"
    severity: str = "block"
    context: str = "[REDACTED]"
    description: str = "test finding"

    def as_dict(self) -> dict[str, object]:
        return {"var_name": self.var_name, "severity": self.severity}


@dataclass
class _FakeVerificationResult:
    passed: bool
    env_findings: list = field(default_factory=list)
    file_findings: list = field(default_factory=list)

    @property
    def blocking_env(self) -> list:
        return [f for f in self.env_findings if f.severity == "block"]

    @property
    def blocking_files(self) -> list:
        return [f for f in self.file_findings if f.severity == "block"]

    @property
    def blocking_count(self) -> int:
        return len(self.blocking_env) + len(self.blocking_files)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.env_findings if f.severity == "warn") + sum(
            1 for f in self.file_findings if f.severity == "warn"
        )


class TestMainCredentialGate:
    @pytest.fixture(autouse=True)
    def _job_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("JOB_ID", "test-job-id")
        monkeypatch.setenv("JOB_DIR", str(tmp_path))
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "foundry")

    async def test_airgap_off_skips_verification_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENSCIENTIST_AIR_GAPPED", raising=False)
        fake_result = {"status": "completed", "iterations": 1, "findings": 0}
        with patch("openscientist.airgap.credential_verifier.verify_airgap_startup") as mock_verify:
            with patch(
                "openscientist.orchestrator.discovery.run_discovery_async",
                new=AsyncMock(return_value=fake_result),
            ):
                exit_code = await entrypoint.main()
        mock_verify.assert_not_called()
        assert exit_code == 0

    async def test_airgap_on_blocking_finding_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        blocking_result = _FakeVerificationResult(passed=False, env_findings=[_FakeFinding()])
        run_discovery_mock = AsyncMock()
        with patch(
            "openscientist.airgap.credential_verifier.verify_airgap_startup",
            return_value=blocking_result,
        ):
            with patch(
                "openscientist.orchestrator.discovery.run_discovery_async",
                new=run_discovery_mock,
            ):
                exit_code = await entrypoint.main()
        run_discovery_mock.assert_not_called()
        assert exit_code == 1

    async def test_airgap_on_clean_env_proceeds_to_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        clean_result = _FakeVerificationResult(passed=True)
        fake_result = {"status": "completed", "iterations": 1, "findings": 0}
        run_discovery_mock = AsyncMock(return_value=fake_result)
        with patch(
            "openscientist.airgap.credential_verifier.verify_airgap_startup",
            return_value=clean_result,
        ):
            with patch(
                "openscientist.orchestrator.discovery.run_discovery_async",
                new=run_discovery_mock,
            ):
                exit_code = await entrypoint.main()
        run_discovery_mock.assert_called_once()
        assert exit_code == 0

    async def test_airgap_on_warning_only_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        warning_result = _FakeVerificationResult(
            passed=True, env_findings=[_FakeFinding(severity="warn")]
        )
        fake_result = {"status": "completed", "iterations": 1, "findings": 0}
        run_discovery_mock = AsyncMock(return_value=fake_result)
        with patch(
            "openscientist.airgap.credential_verifier.verify_airgap_startup",
            return_value=warning_result,
        ):
            with patch(
                "openscientist.orchestrator.discovery.run_discovery_async",
                new=run_discovery_mock,
            ):
                exit_code = await entrypoint.main()
        run_discovery_mock.assert_called_once()
        assert exit_code == 0

"""Tests for the reproducible local quality command runner."""

from __future__ import annotations

from dataclasses import dataclass

from openscientist import quality


@dataclass
class FakeDockerClient:
    available: bool = True
    closed: bool = False

    def ping(self) -> bool:
        return self.available

    def close(self) -> None:
        self.closed = True


def test_docker_preflight_accepts_available_daemon(capsys) -> None:
    client = FakeDockerClient()

    assert quality.require_docker(lambda: client) == 0
    assert client.closed is True
    assert "Docker daemon is available" in capsys.readouterr().out


def test_docker_preflight_reports_explicit_blocker(capsys) -> None:
    def unavailable() -> FakeDockerClient:
        raise RuntimeError("daemon unavailable")

    assert quality.require_docker(unavailable) == quality.BLOCKED_EXIT_CODE
    assert "BLOCKED" in capsys.readouterr().err


def test_docker_preflight_rejects_false_ping(capsys) -> None:
    client = FakeDockerClient(available=False)

    assert quality.require_docker(lambda: client) == quality.BLOCKED_EXIT_CODE
    assert client.closed is True
    assert "BLOCKED" in capsys.readouterr().err


def test_quality_contract_uses_repository_test_paths() -> None:
    assert quality.CONTRACT_TESTS
    assert all(path.startswith("tests/") for path in quality.CONTRACT_TESTS)

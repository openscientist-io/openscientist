"""Tests for the reproducible local quality command runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from openscientist import quality


@dataclass
class FakeDockerClient:
    available: bool = True
    closed: bool = False

    def ping(self) -> bool:
        return self.available

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class FakeCompletedProcess:
    returncode: int


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
    assert all(Path(path).exists() for path in quality.CONTRACT_TESTS)


def test_run_commands_executes_in_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[quality.Command, bool, dict[str, str] | None, bool]] = []
    supplied_env = {"QUALITY_TEST": "enabled"}

    def fake_run(
        command: quality.Command,
        *,
        check: bool,
        env: dict[str, str] | None,
        shell: bool,
    ) -> FakeCompletedProcess:
        calls.append((command, check, env, shell))
        return FakeCompletedProcess(0)

    monkeypatch.setattr(quality.subprocess, "run", fake_run)
    commands = (("tool", "first"), ("tool", "second"))

    assert quality._run_commands("quality-test", commands, env=supplied_env) == 0
    assert calls == [
        (commands[0], False, supplied_env, False),
        (commands[1], False, supplied_env, False),
    ]
    output = capsys.readouterr().out
    assert '["tool", "first"]' in output
    assert "[quality-test] PASSED" in output


def test_run_commands_stops_at_first_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[quality.Command] = []

    def fake_run(
        command: quality.Command,
        *,
        check: bool,
        env: dict[str, str] | None,
        shell: bool,
    ) -> FakeCompletedProcess:
        del check, env, shell
        calls.append(command)
        return FakeCompletedProcess(7 if len(calls) == 2 else 0)

    monkeypatch.setattr(quality.subprocess, "run", fake_run)
    commands = (("tool", "first"), ("tool", "fails"), ("tool", "never"))

    assert quality._run_commands("quality-test", commands) == 7
    assert calls == list(commands[:2])
    assert "FAILED with exit code 7" in capsys.readouterr().err


def test_fast_tier_contains_the_complete_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run_commands(
        label: str,
        commands: tuple[quality.Command, ...],
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        recorded.update(label=label, commands=commands, env=env)
        return 0

    monkeypatch.setattr(quality, "_run_commands", fake_run_commands)

    assert quality.run_fast() == 0
    assert recorded == {
        "label": "quality-fast",
        "commands": (
            ("uv", "lock", "--check"),
            ("git", "-c", "core.safecrlf=false", "diff", "--check"),
            (quality.sys.executable, "-m", "compileall", "-q", "src"),
            (quality.sys.executable, "-m", "ruff", "check", "src/", "tests/"),
            (
                quality.sys.executable,
                "-m",
                "ruff",
                "format",
                "--check",
                "src/",
                "tests/",
            ),
            (quality.sys.executable, "-m", "mypy", "src/openscientist/", "tests/"),
        ),
        "env": None,
    }


def test_contract_tier_uses_only_the_curated_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run_commands(
        label: str,
        commands: tuple[quality.Command, ...],
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        recorded.update(label=label, commands=commands, env=env)
        return 0

    monkeypatch.setattr(quality, "_run_commands", fake_run_commands)

    assert quality.run_contract() == 0
    assert recorded["label"] == "quality-contract"
    assert recorded["commands"] == (
        (quality.sys.executable, "-m", "pytest", "-q", *quality.CONTRACT_TESTS),
    )
    assert recorded["env"] is None


def test_integration_tier_propagates_blocked_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quality, "require_docker", lambda: quality.BLOCKED_EXIT_CODE)

    def unexpected_run(*args: object, **kwargs: object) -> int:
        pytest.fail(f"pytest runner unexpectedly called: {args!r}, {kwargs!r}")

    monkeypatch.setattr(quality, "_run_commands", unexpected_run)

    assert quality.run_integration() == quality.BLOCKED_EXIT_CODE


@pytest.mark.parametrize(
    ("platform", "expects_ryuk_disabled"),
    (("win32", True), ("linux", False)),
)
def test_integration_tier_configures_platform_environment(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expects_ryuk_disabled: bool,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run_commands(
        label: str,
        commands: tuple[quality.Command, ...],
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        recorded.update(label=label, commands=commands, env=env)
        return 0

    monkeypatch.setattr(quality, "require_docker", lambda: 0)
    monkeypatch.setattr(quality, "_run_commands", fake_run_commands)
    monkeypatch.setattr(quality.sys, "platform", platform)

    assert quality.run_integration() == 0
    assert recorded["label"] == "quality-integration"
    commands = recorded["commands"]
    assert isinstance(commands, tuple)
    assert commands == (
        (
            quality.sys.executable,
            "-m",
            "pytest",
            "-m",
            "not network",
            "--cov=src/openscientist",
            "--cov-report=term-missing",
            "--cov-report=xml",
            "--junitxml=pytest-results.xml",
        ),
    )
    env = recorded["env"]
    if expects_ryuk_disabled:
        assert isinstance(env, dict)
        assert env["TESTCONTAINERS_RYUK_DISABLED"] == "true"
    else:
        assert env is None


def test_main_dispatches_requested_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_fast() -> int:
        nonlocal called
        called = True
        return 9

    monkeypatch.setattr(quality, "run_fast", fake_fast)

    assert quality.main(["fast"]) == 9
    assert called is True

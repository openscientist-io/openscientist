"""The host checks the CLI runs before it touches Docker: what each problem
blocks or warns about, and the message that says how to fix it."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles, preflight, wizard
from openscientist.cli.preflight import CommandResult, Finding, Host
from tests.cli.conftest import ready_host

runner = CliRunner()
_GIB = 1024**3


def _info(**fields: object) -> dict[str, object]:
    info: dict[str, object] = {
        "ServerVersion": "29.0.1",
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "MemTotal": 64 * _GIB,
        "DockerRootDir": "/var/lib/docker",
        "OperatingSystem": "Ubuntu 24.04 LTS",
    }
    info.update(fields)
    return info


def _host(
    *,
    info: dict[str, object] | None = None,
    info_error: str | None = None,
    compose: bool = True,
    running: bool = False,
    git: bool = True,
    missing: tuple[str, ...] = (),
    **overrides: object,
) -> Host:
    """A host whose docker answers as told. Everything else is the real host's."""

    def run(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
        if argv[:2] == ("docker", "info"):
            if info_error is not None:
                return CommandResult(1, "", info_error)
            return CommandResult(0, json.dumps(info or _info()), "")
        if argv[:3] == ("docker", "compose", "version"):
            if compose:
                return CommandResult(0, "2.29.1\n", "")
            return CommandResult(1, "", "docker: unknown command: docker compose")
        if argv[:2] == ("docker", "compose") and "ps" in argv:
            return CommandResult(0, "3f2a1b\n" if running else "", "")
        if argv[0] == "git":
            return CommandResult(0, "abc1234\n", "") if git else CommandResult(128, "", "fatal")
        raise AssertionError(f"unexpected command {argv}")

    return Host(
        which=lambda name: None if name in missing else f"/usr/bin/{name}",
        run=run,
        machine=overrides.pop("machine", lambda: "x86_64"),  # type: ignore[arg-type]
        system=overrides.pop("system", lambda: "Linux"),  # type: ignore[arg-type]
        disk_free=overrides.pop("disk_free", lambda path: 500 * _GIB),  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def _blocking(findings: list[Finding]) -> list[str]:
    return [f.message for f in findings if f.blocking]


def _warnings(findings: list[Finding]) -> list[str]:
    return [f.message for f in findings if not f.blocking]


@pytest.fixture
def free_ports(checkout: Path) -> Iterator[None]:
    """Point PORT and POSTGRES_PORT at ports nothing listens on."""
    for key in ("PORT", "POSTGRES_PORT"):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        envfiles.set_file_value(checkout, "env", key, str(port))
    yield


# --- Docker itself ------------------------------------------------------------


def test_a_ready_docker_passes() -> None:
    assert preflight.docker_findings(_host()) == []


def test_a_missing_docker_command_says_how_to_install_it() -> None:
    [finding] = preflight.docker_findings(_host(missing=("docker",)))
    assert finding.blocking
    assert "Docker is not installed" in finding.message
    assert "https://docs.docker.com/get-docker/" in finding.message


def test_an_unreachable_daemon_says_to_start_it() -> None:
    error = "failed to connect to the docker API at unix:///var/run/docker.sock"
    [finding] = preflight.docker_findings(_host(info_error=error))
    assert finding.blocking
    assert "daemon is not running" in finding.message
    assert "systemctl start docker" in finding.message
    assert error in finding.message


def test_a_permission_denied_socket_says_to_join_the_docker_group() -> None:
    error = (
        "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
    )
    [finding] = preflight.docker_findings(_host(info_error=error))
    assert finding.blocking
    assert "usermod -aG docker" in finding.message


def test_a_missing_compose_plugin_says_how_to_install_it() -> None:
    [finding] = preflight.docker_findings(_host(compose=False))
    assert finding.blocking
    assert "docker-compose-plugin" in finding.message


@pytest.mark.parametrize(("version", "too_old"), [("19.03.15", True), ("20.10.0", False)])
def test_an_engine_older_than_20_10_is_refused(version: str, too_old: bool) -> None:
    findings = preflight.docker_findings(_host(info=_info(ServerVersion=version)))
    assert bool(_blocking(findings)) is too_old
    if too_old:
        assert f"Docker Engine {version} is too old" in _blocking(findings)[0]


def test_an_engine_without_a_version_is_refused() -> None:
    [finding] = preflight.docker_findings(_host(info={"OperatingSystem": "podman"}))
    assert finding.blocking
    assert "Docker Engine" in finding.message


# --- what starting the stack needs --------------------------------------------


def test_a_ready_host_starts_without_findings(checkout: Path, free_ports: None) -> None:
    assert preflight.start_findings(checkout, _host()) == []


def test_rootless_docker_is_refused(checkout: Path, free_ports: None) -> None:
    host = _host(info=_info(SecurityOptions=["name=seccomp,profile=builtin", "name=rootless"]))
    [message] = _blocking(preflight.start_findings(checkout, host))
    assert "/var/run/docker.sock" in message


@pytest.mark.parametrize(
    ("machine", "system", "operating_system", "emulated", "refused"),
    [
        ("aarch64", "Linux", "Ubuntu 24.04 LTS", False, True),
        ("aarch64", "Linux", "Ubuntu 24.04 LTS", True, False),
        ("aarch64", "Linux", "Docker Desktop", False, False),
        ("arm64", "Darwin", "Docker Desktop", False, False),
        ("x86_64", "Linux", "Ubuntu 24.04 LTS", False, False),
    ],
)
def test_an_arm64_linux_host_needs_amd64_emulation(
    checkout: Path,
    free_ports: None,
    tmp_path: Path,
    machine: str,
    system: str,
    operating_system: str,
    emulated: bool,
    refused: bool,
) -> None:
    binfmt = tmp_path / "binfmt_misc"
    binfmt.mkdir()
    if emulated:
        (binfmt / "qemu-x86_64").write_text("enabled\n")
    host = _host(
        info=_info(OperatingSystem=operating_system),
        machine=lambda: machine,
        system=lambda: system,
        binfmt_dir=binfmt,
    )
    blocking = _blocking(preflight.start_findings(checkout, host))
    assert bool(blocking) is refused
    if refused:
        assert "tonistiigi/binfmt --install amd64" in blocking[0]


def test_little_free_disk_warns_with_the_data_directory(checkout: Path, free_ports: None) -> None:
    host = _host(disk_free=lambda path: 5 * _GIB)
    [message] = _warnings(preflight.start_findings(checkout, host))
    assert "5 GB free in /var/lib/docker" in message
    assert "docker system prune" in message


def test_docker_desktop_disk_is_not_measured_on_the_host(checkout: Path, free_ports: None) -> None:
    def disk_free(path: Path) -> int:
        raise AssertionError("Docker Desktop keeps its data inside its VM")

    host = _host(info=_info(OperatingSystem="Docker Desktop"), disk_free=disk_free)
    assert preflight.start_findings(checkout, host) == []


def test_too_little_docker_memory_for_one_job_warns(checkout: Path, free_ports: None) -> None:
    envfiles.set_file_value(checkout, "env", "OPENSCIENTIST_AGENT_MEMORY", "6g")
    envfiles.set_file_value(checkout, "env", "OPENSCIENTIST_EXECUTOR_MEMORY", "2g")
    [message] = _warnings(preflight.start_findings(checkout, _host(info=_info(MemTotal=8 * _GIB))))
    assert "Docker can use 8 GB of memory" in message
    assert "8 GB for each job" in message


def test_concurrent_jobs_multiply_the_memory_they_need(checkout: Path, free_ports: None) -> None:
    envfiles.set_file_value(checkout, "env", "OPENSCIENTIST_MAX_CONCURRENT_JOBS", "3")
    findings = preflight.start_findings(checkout, _host(info=_info(MemTotal=24 * _GIB)))
    [message] = _warnings(findings)
    assert "3 jobs at once" in message


def test_a_port_in_use_warns_with_its_setting(checkout: Path, free_ports: None) -> None:
    port = int(envfiles.file_values(checkout, "env")["PORT"])
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", port))
        holder.listen()
        [message] = _warnings(preflight.start_findings(checkout, _host()))
    assert f"Port {port} (PORT) is in use" in message


def test_ports_held_by_the_running_stack_are_not_reported(checkout: Path, free_ports: None) -> None:
    port = int(envfiles.file_values(checkout, "env")["PORT"])
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", port))
        holder.listen()
        assert preflight.start_findings(checkout, _host(running=True)) == []


@pytest.mark.parametrize("git", [False, True])
def test_a_checkout_without_git_warns_about_provenance(
    checkout: Path, free_ports: None, git: bool
) -> None:
    host = _host(git=git) if git else _host(missing=("git",))
    warnings = _warnings(preflight.start_findings(checkout, host))
    assert bool(warnings) is not git
    if not git:
        assert "commit as unknown" in warnings[0]


@pytest.mark.parametrize(
    "key", ["GCP_CREDENTIALS_FILE", "CODEX_AUTH_FILE", "OMP_AUTH_DIR", "PHENIX_HOST_PATH"]
)
def test_a_mounted_path_that_does_not_exist_is_refused(
    checkout: Path, free_ports: None, key: str
) -> None:
    missing = checkout / "nowhere" / key.lower()
    envfiles.set_file_value(checkout, "env", key, str(missing))
    [message] = _blocking(preflight.start_findings(checkout, _host()))
    assert f"{key} points to {missing}, which does not exist" in message
    assert "uv run openscientist setup" in message


def test_a_mounted_path_that_exists_passes(checkout: Path, free_ports: None) -> None:
    auth = checkout / "auth.json"
    auth.write_text("{}")
    envfiles.set_file_value(checkout, "env", "CODEX_AUTH_FILE", str(auth))
    assert preflight.start_findings(checkout, _host()) == []


# --- where the CLI runs them --------------------------------------------------


def test_the_bare_command_stops_before_the_wizard_without_docker(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def wizard_ran(*args: object, **kwargs: object) -> None:
        raise AssertionError("the wizard ran without Docker")

    monkeypatch.setattr(preflight, "HOST", _host(missing=("docker",)))
    monkeypatch.setattr(wizard, "run_setup", wizard_ran)
    result = runner.invoke(cli_app.app, [])
    assert result.exit_code == 1
    assert "Docker is not installed" in result.output
    assert "Traceback" not in result.output


def test_stop_without_docker_says_so_instead_of_crashing(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "HOST", _host(missing=("docker",)))
    result = runner.invoke(cli_app.app, ["stop"])
    assert result.exit_code == 1
    assert "Docker is not installed" in result.output


def test_a_blocking_start_check_stops_before_any_build(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openscientist.cli import stack

    def started(*args: object, **kwargs: object) -> int:
        raise AssertionError("the stack started past a blocking check")

    envfiles.set_file_value(checkout, "env", "CODEX_AUTH_FILE", str(checkout / "missing.json"))
    monkeypatch.setattr(envfiles, "is_configured", lambda root: True)
    monkeypatch.setattr(stack, "start", started)
    monkeypatch.setattr(preflight, "HOST", ready_host())
    result = runner.invoke(cli_app.app, [])
    assert result.exit_code == 1
    assert "CODEX_AUTH_FILE points to" in result.output


def test_warnings_do_not_stop_the_start(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openscientist.cli import stack

    started: list[Path] = []

    def start(root: Path, run: object) -> int:
        started.append(root)
        return 0

    monkeypatch.setattr(envfiles, "is_configured", lambda root: True)
    monkeypatch.setattr(stack, "start", start)
    host = _host(missing=("git",), running=True, disk_free=lambda p: 1 * _GIB)
    monkeypatch.setattr(preflight, "HOST", host)
    result = runner.invoke(cli_app.app, [])
    assert result.exit_code == 0, result.output
    assert started == [checkout]
    assert "commit as unknown" in result.output

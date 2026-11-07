"""The host checks the CLI runs before it touches Docker.

A blocking finding stops the command with a message that says how to fix it,
and a warning is printed and the command goes on. ``docker_findings`` covers
what every Docker command needs and ``start_findings`` what building and
starting the stack needs on top.
"""

from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from rich.console import Console
from rich.text import Text

from openscientist.cli import envfiles
from openscientist.cli.stack import COMPOSE_FILE

#: ``host.docker.internal:host-gateway`` in the compose file needs this engine.
MIN_ENGINE = (20, 10)
#: The four images, which share their base layers, with room for the build cache.
DISK_NEEDED_GB = 20
#: Memory the web, the database and the launcher take beside the jobs.
STACK_MEMORY_MIB = 2048
#: The system socket the launcher service mounts.
DOCKER_SOCKET = "/var/run/docker.sock"
#: Host paths the compose file mounts, each a file or a directory.
MOUNTED_PATHS = ("GCP_CREDENTIALS_FILE", "CODEX_AUTH_FILE", "OMP_AUTH_DIR", "PHENIX_HOST_PATH")

_GIB = 1024**3


class CommandResult(NamedTuple):
    code: int
    stdout: str
    stderr: str


def _run(argv: tuple[str, ...], cwd: Path | None) -> CommandResult:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _port_free(address: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((address, port))
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class Host:
    """What the checks read from the machine."""

    which: Callable[[str], str | None] = shutil.which
    run: Callable[[tuple[str, ...], Path | None], CommandResult] = _run
    machine: Callable[[], str] = platform.machine
    system: Callable[[], str] = platform.system
    disk_free: Callable[[Path], int] = _disk_free
    port_free: Callable[[str, int], bool] = _port_free
    binfmt_dir: Path = field(default=Path("/proc/sys/fs/binfmt_misc"))


HOST = Host()


@dataclass(frozen=True)
class Finding:
    blocking: bool
    message: str


def _docker_info(host: Host) -> tuple[dict[str, object] | None, str]:
    """``docker info`` as a mapping, or None with docker's own error."""
    result = host.run(("docker", "info", "--format", "{{json .}}"), None)
    if result.code != 0:
        return None, (result.stderr or result.stdout).strip()
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, result.stdout.strip()
    return (info if isinstance(info, dict) else None), ""


def _engine_version(info: dict[str, object]) -> tuple[int, int] | None:
    parts = str(info.get("ServerVersion") or "").split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def docker_findings(host: Host = HOST) -> list[Finding]:
    """The docker command, a daemon it can reach, Compose v2 and a recent engine."""
    if host.which("docker") is None:
        return [
            Finding(
                True,
                "Docker is not installed. Install Docker Engine (Linux) or Docker Desktop "
                "(macOS, Windows) from https://docs.docker.com/get-docker/ and run "
                "`uv run openscientist` again.",
            )
        ]
    info, error = _docker_info(host)
    if info is None:
        if "permission denied" in error.lower():
            return [
                Finding(
                    True,
                    "Your user cannot reach the Docker daemon. Run "
                    "`sudo usermod -aG docker $USER`, log out and back in, and try again.\n"
                    f"Docker said: {error}",
                )
            ]
        return [
            Finding(
                True,
                "Docker is installed but its daemon is not running. Start it "
                "(`sudo systemctl start docker`, or open Docker Desktop) and try again.\n"
                f"Docker said: {error}",
            )
        ]
    findings: list[Finding] = []
    if host.run(("docker", "compose", "version", "--short"), None).code != 0:
        findings.append(
            Finding(
                True,
                "Docker Compose v2 is missing. Install the `docker-compose-plugin` package "
                "or update Docker Desktop. The old standalone `docker-compose` is not supported.",
            )
        )
    version = _engine_version(info)
    if version is None:
        findings.append(
            Finding(
                True,
                "The `docker` command does not report a Docker Engine version. OpenScientist "
                "needs Docker Engine 20.10 or newer.",
            )
        )
    elif version < MIN_ENGINE:
        findings.append(
            Finding(
                True,
                f"Docker Engine {info['ServerVersion']} is too old. OpenScientist needs 20.10 "
                "or newer for `host.docker.internal`. Update Docker.",
            )
        )
    return findings


def _rootless(info: dict[str, object]) -> list[Finding]:
    options = info.get("SecurityOptions")
    if isinstance(options, list) and any("name=rootless" in str(o) for o in options):
        return [
            Finding(
                True,
                f"Rootless Docker is not supported, because the launcher mounts the system "
                f"socket {DOCKER_SOCKET}. Use the system Docker daemon.",
            )
        ]
    return []


def _desktop(info: dict[str, object]) -> bool:
    return "docker desktop" in str(info.get("OperatingSystem") or "").lower()


def _emulation(info: dict[str, object], host: Host) -> list[Finding]:
    """An arm64 Linux engine runs the linux/amd64 images only through emulation."""
    arm64 = host.machine().lower() in ("aarch64", "arm64")
    if not arm64 or host.system() != "Linux" or _desktop(info):
        return []
    if (host.binfmt_dir / "qemu-x86_64").exists():
        return []
    return [
        Finding(
            True,
            "This machine is arm64 and the images are built for linux/amd64. Register "
            "emulation with `docker run --privileged --rm tonistiigi/binfmt --install amd64` "
            "and try again.",
        )
    ]


def _disk(info: dict[str, object], host: Host) -> list[Finding]:
    """Free space where the engine keeps its images, when it is on this host."""
    root = info.get("DockerRootDir")
    if host.system() != "Linux" or _desktop(info) or not root:
        return []
    try:
        free = host.disk_free(Path(str(root)))
    except OSError:
        return []
    if free >= DISK_NEEDED_GB * _GIB:
        return []
    return [
        Finding(
            False,
            f"Docker has {free // _GIB} GB free in {root}, and the images need about "
            f"{DISK_NEEDED_GB} GB. Free space with `docker system prune`, or move Docker's "
            "data directory.",
        )
    ]


def _memory(info: dict[str, object], values: dict[str, str]) -> list[Finding]:
    """Memory the engine has against what the configured jobs ask for."""
    from openscientist.admin_settings import parse_docker_memory_mib

    total = int(str(info.get("MemTotal") or 0))
    if total <= 0:
        return []
    try:
        per_job = parse_docker_memory_mib(
            values.get("OPENSCIENTIST_AGENT_MEMORY") or "8g", "OPENSCIENTIST_AGENT_MEMORY"
        ) + parse_docker_memory_mib(
            values.get("OPENSCIENTIST_EXECUTOR_MEMORY") or "2g", "OPENSCIENTIST_EXECUTOR_MEMORY"
        )
        jobs = max(1, int(values.get("OPENSCIENTIST_MAX_CONCURRENT_JOBS") or "1"))
    except ValueError:
        return []
    needed_mib = per_job * jobs + STACK_MEMORY_MIB
    if total // (1024 * 1024) >= needed_mib:
        return []
    running = f"{jobs} jobs at once" if jobs > 1 else "one job"
    return [
        Finding(
            False,
            f"Docker can use {total // _GIB} GB of memory, and {running} need "
            f"{round(needed_mib / 1024)} GB with {round(per_job / 1024)} GB for each job. "
            "Give Docker more memory (in Docker Desktop, Settings and then Resources), or "
            "lower the container memory under Admin, Settings.",
        )
    ]


def _ports(root: Path, values: dict[str, str], host: Host) -> list[Finding]:
    """Ports the stack publishes, unless the stack is already up and holds them."""
    running = host.run(
        ("docker", "compose", "-f", COMPOSE_FILE, "ps", "--status", "running", "-q"), root
    )
    if running.code == 0 and running.stdout.strip():
        return []
    findings: list[Finding] = []
    for key, address, default in (
        ("PORT", values.get("OPENSCIENTIST_BIND_ADDRESS") or "127.0.0.1", "8080"),
        ("POSTGRES_PORT", "127.0.0.1", "5432"),
    ):
        try:
            port = int(values.get(key) or default)
        except ValueError:
            continue
        if not host.port_free(address, port):
            findings.append(
                Finding(
                    False,
                    f"Port {port} ({key}) is in use by another program. Stop it, or set "
                    f"{key} to a free port in .env.",
                )
            )
    return findings


def _git(root: Path, host: Host) -> list[Finding]:
    if (
        host.which("git") is not None
        and host.run(("git", "rev-parse", "--short", "HEAD"), root).code == 0
    ):
        return []
    return [
        Finding(
            False,
            "This checkout has no git history or git is not installed, so job provenance "
            "records the commit as unknown.",
        )
    ]


def _mounted_paths(values: dict[str, str]) -> list[Finding]:
    """Docker creates an empty directory for a missing mount source, which the
    provider later fails on, so a set path must exist."""
    return [
        Finding(
            True,
            f"{key} points to {values[key]}, which does not exist. Fix it in .env or re-run "
            "`uv run openscientist setup`.",
        )
        for key in MOUNTED_PATHS
        if values.get(key) and not Path(values[key]).exists()
    ]


def start_findings(root: Path, host: Host = HOST) -> list[Finding]:
    """What building and starting the stack needs beyond a working Docker."""
    info, _ = _docker_info(host)
    values = envfiles.file_values(root, "env")
    findings = _mounted_paths(values)
    if info is not None:
        findings += _rootless(info)
        findings += _emulation(info, host)
        findings += _disk(info, host)
        findings += _memory(info, values)
    findings += _ports(root, values, host)
    findings += _git(root, host)
    return findings


def report(findings: list[Finding], console: Console) -> bool:
    """Print the findings, blocking ones first; True when none blocks."""
    for finding in sorted(findings, key=lambda f: not f.blocking):
        console.print(Text(finding.message, style="red" if finding.blocking else "yellow"))
    return not any(finding.blocking for finding in findings)

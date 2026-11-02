"""Build, start and stop the compose stack."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from openscientist.cli import envfiles

COMPOSE_FILE = "docker-compose.yml"
WEB_SERVICE = "openscientist"
BUILD_PLATFORM = "linux/amd64"
HEALTH_TIMEOUT = 300.0

console = Console()

#: Runs a command, streams its output and returns its exit code.
CommandRunner = Callable[[tuple[str, ...], Mapping[str, str] | None], int]


def run_command(argv: tuple[str, ...], env: Mapping[str, str] | None = None) -> int:
    """Run the command in the checkout, streaming its output; return its exit code."""
    merged_env = {**os.environ, **env} if env else os.environ
    return subprocess.run(argv, env=merged_env).returncode


def build_images(run: CommandRunner, root: Path) -> int:
    """The four image builds with the Makefile's tags and platform; 0 on success."""
    values = envfiles.file_values(root, "env")
    base_image = values.get("OPENSCIENTIST_BASE_IMAGE") or "openscientist-base:latest"
    executor_image = values.get("OPENSCIENTIST_EXECUTOR_IMAGE") or "openscientist-executor:latest"
    agent_image = values.get("OPENSCIENTIST_AGENT_IMAGE") or "openscientist-agent:latest"
    platform_env = {"DOCKER_DEFAULT_PLATFORM": BUILD_PLATFORM}
    code = run(("docker", "build", "-f", "Dockerfile.base", "-t", base_image, "."), platform_env)
    if code:
        return _failed("the base image", code)
    code = run(
        (
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE,
            "build",
            "--build-arg",
            f"OPENSCIENTIST_COMMIT={_commit()}",
            "--build-arg",
            f"BUILD_TIME={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        ),
        None,
    )
    if code:
        return _failed("the web image", code)
    code = run(
        ("docker", "build", "-f", "Dockerfile.executor", "-t", executor_image, "."), platform_env
    )
    if code:
        return _failed("the executor image", code)
    code = run(("docker", "build", "-f", "Dockerfile.agent", "-t", agent_image, "."), platform_env)
    if code:
        return _failed("the agent image", code)
    return 0


def start(
    root: Path, run: CommandRunner = run_command, health_timeout: float = HEALTH_TIMEOUT
) -> int:
    """Build the four images, start the stack, wait for /health, print the base URL."""
    code = build_images(run, root)
    if code:
        return code
    code = run(
        ("docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--remove-orphans", WEB_SERVICE), None
    )
    if code:
        console.print(f"[red]Starting the stack failed (exit {code}).[/red]")
        return code
    values = envfiles.file_values(root, "env")
    port = int(values.get("PORT") or "8080")
    base_url = values.get("OPENSCIENTIST_BASE_URL") or f"http://localhost:{port}"
    console.print("Waiting for the stack to become healthy...")
    if not wait_for_health(f"http://localhost:{port}", health_timeout):
        console.print(
            "[red]The stack did not become healthy in time. Check the logs with `docker compose logs`.[/red]"
        )
        return 1
    console.print(f"OpenScientist is up at {base_url}")
    return 0


def stop(root: Path, run: CommandRunner = run_command) -> int:
    """Stop the stack, keeping the data volume."""
    code = run(("docker", "compose", "-f", COMPOSE_FILE, "down", "--remove-orphans"), None)
    if code:
        console.print(f"[red]Stopping the stack failed (exit {code}).[/red]")
    else:
        console.print("OpenScientist stopped.")
    return code


def wait_for_health(base_url: str, timeout: float = HEALTH_TIMEOUT, interval: float = 3.0) -> bool:
    """Poll ``{base_url}/health`` until it answers or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=5) as response:
                if 200 <= response.status < 300:
                    return True
        except OSError:
            pass
        time.sleep(interval)
    return False


def _commit() -> str:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--short", "HEAD"), capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def _failed(what: str, code: int) -> int:
    console.print(f"[red]Building {what} failed (exit {code}).[/red]")
    return code

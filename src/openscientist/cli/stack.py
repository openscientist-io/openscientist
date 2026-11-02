"""Build, start and stop the compose stack."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import click
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
    _offer_pubmed_mirror(root, run, values)
    return 0


def stop(root: Path, run: CommandRunner = run_command) -> int:
    """Stop the stack, keeping the data volume."""
    code = run(("docker", "compose", "-f", COMPOSE_FILE, "down", "--remove-orphans"), None)
    if code:
        console.print(f"[red]Stopping the stack failed (exit {code}).[/red]")
    else:
        console.print("OpenScientist stopped.")
    return code


#: The documented load command for the PubMed baseline, run as the migrate service.
PUBMED_MIRROR_LOAD = (
    "docker",
    "compose",
    "-f",
    COMPOSE_FILE,
    "run",
    "--rm",
    "migrate",
    "python",
    "-m",
    "openscientist.pubmed_mirror",
)


def pubmed_mirror_count(root: Path) -> int | None:
    """The row count of pubmed_articles, or None when it cannot be read."""
    values = envfiles.file_values(root, "env")
    user = values.get("POSTGRES_USER") or "openscientist"
    db = values.get("POSTGRES_DB") or "openscientist"
    out = _compose_exec_postgres(
        root, ("psql", "-U", user, "-d", db, "-tAc", "SELECT count(*) FROM pubmed_articles;")
    )
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _compose_exec_postgres(root: Path, argv: tuple[str, ...]) -> str:
    """Run a command in the postgres service; return its stdout, or empty on failure."""
    proc = subprocess.run(
        ("docker", "compose", "-f", COMPOSE_FILE, "exec", "-T", "postgres", *argv),
        cwd=root,
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _offer_pubmed_mirror(root: Path, run: CommandRunner, values: dict[str, str]) -> None:
    """When air-gapped and the mirror is empty, offer to load the PubMed baseline."""
    if not _airgapped_on(values):
        return
    if pubmed_mirror_count(root) != 0:
        return
    console.print("The PubMed mirror is empty. Loading it is a long download.")
    if not _confirm("Load the PubMed mirror now? (y/N) "):
        console.print(
            "The stack stays running. Load it later with "
            "`docker compose run --rm migrate python -m openscientist.pubmed_mirror`."
        )
        return
    code = run(PUBMED_MIRROR_LOAD, None)
    if code:
        console.print(f"[red]Loading the PubMed mirror failed (exit {code}).[/red]")
    else:
        console.print("Loaded the PubMed mirror.")


def _airgapped_on(values: dict[str, str]) -> bool:
    return values.get("OPENSCIENTIST_AIRGAPPED", "").strip().lower() in (
        "true",
        "1",
        "yes",
        "y",
        "on",
    )


def _confirm(prompt: str) -> bool:
    value = click.prompt(prompt, default="n", show_default=False).strip().lower()
    return value in ("y", "yes")


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

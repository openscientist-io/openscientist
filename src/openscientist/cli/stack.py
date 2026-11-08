"""Build, start and stop the compose stack."""

from __future__ import annotations

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

#: Runs a command in the checkout with exactly the environment it gets,
#: streams its output and returns its exit code.
CommandRunner = Callable[[tuple[str, ...], Mapping[str, str]], int]


def run_command(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
    """Run the command in the checkout with ``env``, streaming its output; return its exit code."""
    return subprocess.run(argv, env=dict(env)).returncode


def _image_id(image: str) -> str:
    """The local config digest of a built image, or empty when unreadable."""
    proc = subprocess.run(
        ("docker", "image", "inspect", "--format", "{{.Id}}", image),
        env=envfiles.docker_env(Path.cwd()),
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def record_image_digests(root: Path, image_id: Callable[[str], str] | None = None) -> int:
    """Record each image's digest in .env after the builds.

    The launcher accepts only the digests recorded here, so a rebuilt image is
    trusted again only through a build that re-records it.
    """
    image_id = image_id or _image_id
    values = envfiles.file_values(root, "env")
    agent_image = values.get("OPENSCIENTIST_AGENT_IMAGE") or "openscientist-agent:latest"
    executor_image = values.get("OPENSCIENTIST_EXECUTOR_IMAGE") or "openscientist-executor:latest"
    for tag, key in (
        (agent_image, "AGENT_IMAGE_DIGESTS"),
        (executor_image, "EXECUTOR_IMAGE_DIGESTS"),
    ):
        digest = image_id(tag)
        if not digest:
            console.print(f"[red]Could not read the digest of {tag}.[/red]")
            return 1
        envfiles.set_file_value(root, "env", key, digest)
    return 0


def build_images(run: CommandRunner, root: Path) -> int:
    """The four image builds, the tags from .env with their :latest fallbacks and the
    default platform on every build; 0 on success."""
    values = envfiles.file_values(root, "env")
    base_image = _base_image(values)
    executor_image = values.get("OPENSCIENTIST_EXECUTOR_IMAGE") or "openscientist-executor:latest"
    agent_image = values.get("OPENSCIENTIST_AGENT_IMAGE") or "openscientist-agent:latest"
    build_env = envfiles.docker_env(root, {"DOCKER_DEFAULT_PLATFORM": BUILD_PLATFORM})
    code = run(("docker", "build", "-f", "Dockerfile.base", "-t", base_image, "."), build_env)
    if code:
        return _failed("the base image", code)
    # One commit and build time for every image, so the images report the same
    # build as the web and the launcher.
    build_args = (
        "--build-arg",
        f"OPENSCIENTIST_COMMIT={_commit()}",
        "--build-arg",
        f"BUILD_TIME={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    )
    code = run(
        (
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE,
            "build",
            *build_args,
        ),
        build_env,
    )
    if code:
        return _failed("the web image", code)
    code = run(
        ("docker", "build", "-f", "Dockerfile.executor", "-t", executor_image, *build_args, "."),
        build_env,
    )
    if code:
        return _failed("the executor image", code)
    code = run(
        ("docker", "build", "-f", "Dockerfile.agent", "-t", agent_image, *build_args, "."),
        build_env,
    )
    if code:
        return _failed("the agent image", code)
    code = record_image_digests(root)
    if code:
        console.print("[red]Recording the image digests failed.[/red]")
        return code
    return 0


def _base_image(values: Mapping[str, str]) -> str:
    return values.get("OPENSCIENTIST_BASE_IMAGE") or "openscientist-base:latest"


#: The jobs group every image shares (gid 1001), and the setgid mode of the tree it owns.
JOBS_GROUP = 1001
JOBS_DIR_MODE = "2770"


def prepare_jobs_dir(root: Path, run: CommandRunner, image: str) -> int:
    """Create ``<root>/jobs`` and give it to the jobs group as a setgid tree.

    Compose refuses the launcher's jobs mount for a missing directory, and the
    non-root web creates job directories in it through the group. A root
    one-shot in ``image`` sets the group, so this works for an operator outside
    the jobs group and on a tree an older root web created."""
    jobs = root.resolve() / "jobs"
    jobs.mkdir(exist_ok=True)
    return run(
        (
            "docker",
            "run",
            "--rm",
            "--user",
            "root",
            "-v",
            f"{jobs}:/jobs",
            image,
            "sh",
            "-c",
            f"chgrp {JOBS_GROUP} /jobs && chmod {JOBS_DIR_MODE} /jobs",
        ),
        envfiles.docker_env(root),
    )


def start(
    root: Path, run: CommandRunner = run_command, health_timeout: float = HEALTH_TIMEOUT
) -> int:
    """Record the checkout path, build the four images, prepare the jobs
    directory, start the stack, wait for /health, print the base URL."""
    envfiles.record_host_project_dir(root)
    code = build_images(run, root)
    if code:
        return code
    code = prepare_jobs_dir(root, run, _base_image(envfiles.file_values(root, "env")))
    if code:
        console.print(f"[red]Preparing the jobs directory failed (exit {code}).[/red]")
        return code
    code = run(
        ("docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--remove-orphans", WEB_SERVICE),
        envfiles.docker_env(root),
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
    code = run(
        ("docker", "compose", "-f", COMPOSE_FILE, "down", "--remove-orphans"),
        envfiles.docker_env(root),
    )
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
        env=envfiles.docker_env(root),
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
    code = run(PUBMED_MIRROR_LOAD, envfiles.docker_env(root))
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

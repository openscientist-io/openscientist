"""Build, start and stop the compose stack."""

from __future__ import annotations

import re
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

#: Each image's .env key and its tag when unset. Tags are host-wide, so a second checkout sets its own.
IMAGE_TAGS = {
    "OPENSCIENTIST_BASE_IMAGE": "openscientist-base:latest",
    "OPENSCIENTIST_EXECUTOR_IMAGE": "openscientist-executor:latest",
    "OPENSCIENTIST_AGENT_IMAGE": "openscientist-agent:latest",
    "OPENSCIENTIST_LAUNCHER_IMAGE": "openscientist-launcher:latest",
}

#: The registry the images workflow pushes to, and each image's .env key and repository there.
REGISTRY = "ghcr.io/openscientist-io"
REGISTRY_IMAGES = {
    "OPENSCIENTIST_BASE_IMAGE": "openscientist-base",
    "OPENSCIENTIST_EXECUTOR_IMAGE": "openscientist-executor",
    "OPENSCIENTIST_AGENT_IMAGE": "openscientist-agent",
    "OPENSCIENTIST_LAUNCHER_IMAGE": "openscientist-launcher",
    "OPENSCIENTIST_WEB_IMAGE": "openscientist-web",
}
#: Database dumps a deploy takes, kept in the checkout, newest first by name.
BACKUPS_DIR = "backups"
BACKUPS_KEPT = 10
_DUMP_IN_CONTAINER = "/tmp/openscientist-deploy.dump"
_DOCKER_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")

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
    agent_image = image_tag(values, "OPENSCIENTIST_AGENT_IMAGE")
    executor_image = image_tag(values, "OPENSCIENTIST_EXECUTOR_IMAGE")
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
    """The four image builds, the tags from .env with their :latest fallbacks, every
    image after the base built FROM that base, and the default platform on every
    build; 0 on success. Compose tags the launcher with OPENSCIENTIST_LAUNCHER_IMAGE."""
    values = envfiles.file_values(root, "env")
    base_image = image_tag(values, "OPENSCIENTIST_BASE_IMAGE")
    executor_image = image_tag(values, "OPENSCIENTIST_EXECUTOR_IMAGE")
    agent_image = image_tag(values, "OPENSCIENTIST_AGENT_IMAGE")
    build_env = envfiles.docker_env(root, {"DOCKER_DEFAULT_PLATFORM": BUILD_PLATFORM})
    code = run(("docker", "build", "-f", "Dockerfile.base", "-t", base_image, "."), build_env)
    if code:
        return _failed("the base image", code)
    # The base just built, and one commit and build time for every image, so the
    # images report the same build as the web and the launcher.
    build_args = (
        "--build-arg",
        f"BASE_IMAGE={base_image}",
        "--build-arg",
        f"OPENSCIENTIST_COMMIT={_commit()}",
        "--build-arg",
        f"BUILD_TIME={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    )
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
    # After the digests, because compose refuses to read its file without them.
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
    return 0


def image_tag(values: Mapping[str, str], key: str) -> str:
    """The tag .env gives the image under ``key``, or its default."""
    return values.get(key) or IMAGE_TAGS[key]


#: The jobs group every image shares (gid 1001), and the setgid mode of the tree it owns.
JOBS_GROUP = 1001
JOBS_DIR_MODE = "2770"
#: Root shell for the jobs tree. Only entries outside the group or short of its
#: rights are touched, so a re-run is quick, and links are regrouped, never followed.
JOBS_TREE_SCRIPT = (
    f"chgrp {JOBS_GROUP} /jobs && chmod {JOBS_DIR_MODE} /jobs && "
    f"find /jobs -mindepth 1 \\( ! -group {JOBS_GROUP} -o -type d ! -perm -2070 "
    "-o -type f ! -perm -060 \\) "
    f"\\( -type l -exec chgrp -h {JOBS_GROUP} {{}} + "
    f"-o -type d -exec chgrp {JOBS_GROUP} {{}} + -exec chmod g+rwxs {{}} + "
    f"-o -exec chgrp {JOBS_GROUP} {{}} + -exec chmod g+rwX {{}} + \\) "
    "-printf . > /tmp/changed && "
    'echo "Gave $(wc -c < /tmp/changed) entries of the jobs tree to the jobs group."'
)


def prepare_jobs_dir(root: Path, run: CommandRunner, image: str) -> int:
    """Create ``<root>/jobs`` and give it to the jobs group as a setgid tree.

    Compose refuses the launcher's jobs mount for a missing directory, and the
    non-root web creates, writes and deletes job directories through the group.
    A root one-shot in ``image`` sets the group, so this works for an operator
    outside the jobs group and on a tree an older root web created."""
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
            JOBS_TREE_SCRIPT,
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
    code = prepare_jobs_dir(
        root, run, image_tag(envfiles.file_values(root, "env"), "OPENSCIENTIST_BASE_IMAGE")
    )
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
    base_url = values.get("OPENSCIENTIST_BASE_URL") or _local_url(values)
    if not _healthy(values, health_timeout):
        return 1
    console.print(f"OpenScientist is up at {base_url}")
    _offer_pubmed_mirror(root, run, values)
    return 0


def deploy(
    root: Path, tag: str, run: CommandRunner = run_command, health_timeout: float = HEALTH_TIMEOUT
) -> int:
    """Run the stack on the registry images tagged ``tag``.

    .env names the pulled images and their digests before the database dump,
    because compose reads no file without the digests, and gets its previous
    values back when the dump fails. A stack that does not start or turn
    healthy is started again on the images it ran before, and the deploy still
    fails. A migration that already ran stays, and the dump is what restores it."""
    if not _DOCKER_TAG.fullmatch(tag):
        console.print(f"[red]{tag!r} is not a Docker image tag.[/red]")
        return 2
    images = {key: f"{REGISTRY}/{repository}:{tag}" for key, repository in REGISTRY_IMAGES.items()}
    env = envfiles.docker_env(root)
    for image in images.values():
        code = run(("docker", "pull", image), env)
        if code:
            console.print(f"[red]Pulling {image} failed (exit {code}).[/red]")
            return code
    envfiles.record_host_project_dir(root)
    values = envfiles.file_values(root, "env")
    previous = {key: values.get(key, "") for key in (*images, *_DIGEST_KEYS)}
    for key, image in images.items():
        envfiles.set_file_value(root, "env", key, image)
    code = record_image_digests(root)
    if not code:
        code = dump_database(root, run, tag)
        if code:
            console.print(f"[red]Dumping the database failed (exit {code}).[/red]")
    if code:
        _restore(root, previous)
        return code
    if _start_pulled(root, run, health_timeout) == 0:
        console.print(f"Deployed {tag}.")
        return 0
    console.print("[red]The deploy failed. Starting the previous images again.[/red]")
    _restore(root, previous)
    if _start_pulled(root, run, health_timeout):
        console.print("[red]The previous images did not come back either.[/red]")
    return 1


_DIGEST_KEYS = ("AGENT_IMAGE_DIGESTS", "EXECUTOR_IMAGE_DIGESTS")


def _restore(root: Path, previous: Mapping[str, str]) -> None:
    for key, value in previous.items():
        envfiles.set_file_value(root, "env", key, value)


def dump_database(root: Path, run: CommandRunner, tag: str) -> int:
    """Dump the stack's database into the backups directory, keeping the newest
    ``BACKUPS_KEPT`` dumps; 0 on success."""
    values = envfiles.file_values(root, "env")
    user = values.get("POSTGRES_USER") or "openscientist"
    database = values.get("POSTGRES_DB") or "openscientist"
    backups = root / BACKUPS_DIR
    backups.mkdir(exist_ok=True)
    target = backups / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{tag}.dump"
    compose = ("docker", "compose", "-f", COMPOSE_FILE)
    env = envfiles.docker_env(root)
    for argv in (
        (*compose, "up", "-d", "--wait", "postgres"),
        (
            *compose,
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            user,
            "-d",
            database,
            "-Fc",
            "-f",
            _DUMP_IN_CONTAINER,
        ),
        (*compose, "cp", f"postgres:{_DUMP_IN_CONTAINER}", str(target)),
        (*compose, "exec", "-T", "postgres", "rm", "-f", _DUMP_IN_CONTAINER),
    ):
        code = run(argv, env)
        if code:
            return code
    for old in sorted(backups.glob("*.dump"), reverse=True)[BACKUPS_KEPT:]:
        old.unlink()
    return 0


def _start_pulled(root: Path, run: CommandRunner, health_timeout: float) -> int:
    """Start the stack on the images and digests .env names, without building; 0 once healthy."""
    values = envfiles.file_values(root, "env")
    code = prepare_jobs_dir(root, run, image_tag(values, "OPENSCIENTIST_BASE_IMAGE"))
    if code:
        console.print(f"[red]Preparing the jobs directory failed (exit {code}).[/red]")
        return code
    code = run(
        (
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE,
            "up",
            "-d",
            "--remove-orphans",
            "--no-build",
            WEB_SERVICE,
        ),
        envfiles.docker_env(root),
    )
    if code:
        console.print(f"[red]Starting the stack failed (exit {code}).[/red]")
        return code
    return 0 if _healthy(values, health_timeout) else 1


def _local_url(values: Mapping[str, str]) -> str:
    return f"http://localhost:{int(values.get('PORT') or '8080')}"


def _healthy(values: Mapping[str, str], timeout: float) -> bool:
    """Wait for the stack's /health on this host, reporting a stack that never answers."""
    console.print("Waiting for the stack to become healthy...")
    if wait_for_health(_local_url(values), timeout):
        return True
    console.print(
        "[red]The stack did not become healthy in time. Check the logs with `docker compose logs`.[/red]"
    )
    return False


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

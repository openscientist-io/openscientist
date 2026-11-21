"""`openscientist deploy`: run a stack on registry images through an injected command runner."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles, stack
from tests.cli.conftest import REPO_ROOT
from tests.cli.test_compose_env import _configure

TAG = "sha-0123456789ab"
PREVIOUS = {
    "OPENSCIENTIST_BASE_IMAGE": "openscientist-base:prod",
    "OPENSCIENTIST_EXECUTOR_IMAGE": "openscientist-executor:prod",
    "OPENSCIENTIST_AGENT_IMAGE": "openscientist-agent:prod",
    "OPENSCIENTIST_LAUNCHER_IMAGE": "openscientist-launcher:prod",
}
PULLED = {
    "OPENSCIENTIST_BASE_IMAGE": f"ghcr.io/openscientist-io/openscientist-base:{TAG}",
    "OPENSCIENTIST_EXECUTOR_IMAGE": f"ghcr.io/openscientist-io/openscientist-executor:{TAG}",
    "OPENSCIENTIST_AGENT_IMAGE": f"ghcr.io/openscientist-io/openscientist-agent:{TAG}",
    "OPENSCIENTIST_LAUNCHER_IMAGE": f"ghcr.io/openscientist-io/openscientist-launcher:{TAG}",
    "OPENSCIENTIST_WEB_IMAGE": f"ghcr.io/openscientist-io/openscientist-web:{TAG}",
}
COMPOSE = ("docker", "compose", "-f", "docker-compose.yml")


class Docker:
    """A command runner standing in for docker: records every call and fails
    the calls a test names."""

    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.failing = failing

    def __call__(self, argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        self.calls.append(argv)
        return 1 if any(word in argv for word in self.failing) else 0

    def index(self, prefix: tuple[str, ...]) -> int:
        return next(i for i, argv in enumerate(self.calls) if argv[: len(prefix)] == prefix)

    def ups(self) -> list[tuple[str, ...]]:
        return [argv for argv in self.calls if argv[:5] == (*COMPOSE, "up")]


@pytest.fixture
def configured(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configured checkout running locally built images, whose digests read
    from the image name."""
    _configure(checkout)
    for key, value in PREVIOUS.items():
        envfiles.set_file_value(checkout, "env", key, value)
    monkeypatch.setattr(stack, "_image_id", lambda image: f"id-of-{image}")
    return checkout


def _env(checkout: Path) -> dict[str, str]:
    return envfiles.file_values(checkout, "env")


def test_deploy_runs_the_tagged_registry_images_after_a_database_dump(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    docker = Docker()

    assert stack.deploy(configured, TAG, docker) == 0

    values = _env(configured)
    assert {key: values[key] for key in PULLED} == PULLED
    assert values["AGENT_IMAGE_DIGESTS"] == f"id-of-{PULLED['OPENSCIENTIST_AGENT_IMAGE']}"
    assert values["EXECUTOR_IMAGE_DIGESTS"] == f"id-of-{PULLED['OPENSCIENTIST_EXECUTOR_IMAGE']}"
    pulls = [argv[2] for argv in docker.calls if argv[:2] == ("docker", "pull")]
    assert sorted(pulls) == sorted(PULLED.values())
    dump = docker.index((*COMPOSE, "exec", "-T", "postgres", "pg_dump"))
    up = docker.index((*COMPOSE, "up", "-d", "--remove-orphans", "--no-build"))
    assert max(docker.calls.index(("docker", "pull", ref)) for ref in pulls) < dump < up
    assert docker.ups()[-1][-1] == stack.WEB_SERVICE
    assert not any("build" in argv for argv in docker.calls)


def test_deploy_copies_the_dump_into_the_backups_directory(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    docker = Docker()

    assert stack.deploy(configured, TAG, docker) == 0

    copy = docker.calls[docker.index((*COMPOSE, "cp"))]
    target = Path(copy[-1])
    assert target.parent == configured / "backups"
    assert target.name.endswith(f"-{TAG}.dump")


def test_deploy_keeps_the_newest_dumps(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    backups = configured / "backups"
    backups.mkdir()
    older = [backups / f"20260101T0000{n:02d}Z-sha-old.dump" for n in range(stack.BACKUPS_KEPT)]
    for path in older:
        path.write_bytes(b"dump")

    def docker(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        if argv[4:5] == ("cp",):
            Path(argv[-1]).write_bytes(b"dump")
        return 0

    assert stack.deploy(configured, TAG, docker) == 0

    kept = sorted(path.name for path in backups.iterdir())
    assert len(kept) == stack.BACKUPS_KEPT
    assert older[0].name not in kept
    assert kept[-1].endswith(f"-{TAG}.dump")


def test_deploy_dumps_a_checkout_only_setup_has_configured(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compose reads no file without the digests and the project directory, which
    setup alone does not record, so they are in .env before the first dump."""
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    for key in ("AGENT_IMAGE_DIGESTS", "EXECUTOR_IMAGE_DIGESTS", "OPENSCIENTIST_HOST_PROJECT_DIR"):
        envfiles.set_file_value(configured, "env", key, "")
    at_dump: dict[str, str] = {}

    def docker(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        if "pg_dump" in argv:
            at_dump.update(_env(configured))
        return 0

    assert stack.deploy(configured, TAG, docker) == 0

    assert at_dump["AGENT_IMAGE_DIGESTS"] == f"id-of-{PULLED['OPENSCIENTIST_AGENT_IMAGE']}"
    assert at_dump["EXECUTOR_IMAGE_DIGESTS"] == f"id-of-{PULLED['OPENSCIENTIST_EXECUTOR_IMAGE']}"
    assert at_dump["OPENSCIENTIST_HOST_PROJECT_DIR"] == str(configured)


@pytest.mark.parametrize("failing", ["pull", "pg_dump", "cp"])
def test_deploy_stops_before_starting_and_keeps_the_running_images(
    configured: Path, failing: str
) -> None:
    envfiles.set_file_value(configured, "env", "AGENT_IMAGE_DIGESTS", "running-agent")
    envfiles.set_file_value(configured, "env", "EXECUTOR_IMAGE_DIGESTS", "running-executor")
    docker = Docker(failing=(failing,))

    assert stack.deploy(configured, TAG, docker) != 0

    values = _env(configured)
    assert {key: values[key] for key in PREVIOUS} == PREVIOUS
    assert not values.get("OPENSCIENTIST_WEB_IMAGE")
    assert values["AGENT_IMAGE_DIGESTS"] == "running-agent"
    assert values["EXECUTOR_IMAGE_DIGESTS"] == "running-executor"
    assert not any("--no-build" in argv for argv in docker.calls)


@pytest.mark.parametrize("failure", ["health", "up"])
def test_a_failed_deploy_starts_the_previous_images_again(
    configured: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    healthy = iter([failure != "health", True])
    monkeypatch.setattr(
        stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: next(healthy)
    )
    running = {
        key: _env(configured)[key] for key in ("AGENT_IMAGE_DIGESTS", "EXECUTOR_IMAGE_DIGESTS")
    }
    ups = 0

    def docker(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        nonlocal ups
        if argv[4:5] == ("up",) and argv[-1] == stack.WEB_SERVICE:
            ups += 1
            return 1 if failure == "up" and ups == 1 else 0
        return 0

    assert stack.deploy(configured, TAG, docker) == 1

    values = _env(configured)
    assert {key: values[key] for key in PREVIOUS} == PREVIOUS
    assert not values.get("OPENSCIENTIST_WEB_IMAGE")
    assert {key: values[key] for key in running} == running
    assert ups == 2


def test_a_rollback_after_the_new_images_migrated_still_starts_the_previous_images(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The previous web image does not know a revision the new one applied, so
    its `migrate` fails and compose then never starts the web service."""
    healthy = iter([False, True])
    monkeypatch.setattr(
        stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: next(healthy)
    )
    migrated = False
    previous_started = False

    def docker(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        nonlocal migrated, previous_started
        if argv[4:5] != ("up",) or stack.WEB_SERVICE not in argv:
            return 0
        new_images = bool(_env(configured).get("OPENSCIENTIST_WEB_IMAGE"))
        if "--no-deps" not in argv:
            if new_images:
                migrated = True
            elif migrated:
                return 1
        previous_started = not new_images
        return 0

    assert stack.deploy(configured, TAG, docker) == 1

    assert migrated
    assert previous_started


@pytest.mark.parametrize("existing", [False, True])
def test_deploy_keeps_its_dumps_readable_only_by_the_stack_user(
    configured: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    backups = configured / "backups"
    if existing:
        backups.mkdir(mode=0o755)
    dumps: list[Path] = []

    def docker(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        if argv[4:5] == ("cp",):
            dumps.append(Path(argv[-1]))
            dumps[-1].write_bytes(b"dump")
            dumps[-1].chmod(0o644)
        return 0

    assert stack.deploy(configured, TAG, docker) == 0

    assert backups.stat().st_mode & 0o777 == 0o700
    assert [dump.stat().st_mode & 0o777 for dump in dumps] == [0o600]


@pytest.mark.parametrize("tag", ["", "latest\nPORT=1", "a b", "-flag", "x" * 129])
def test_deploy_refuses_a_tag_docker_would_not_accept(configured: Path, tag: str) -> None:
    docker = Docker()

    assert stack.deploy(configured, tag, docker) != 0

    assert docker.calls == []
    assert {key: _env(configured)[key] for key in PREVIOUS} == PREVIOUS


def test_the_deploy_command_runs_a_configured_checkout_on_the_tagged_images(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    docker = Docker()
    monkeypatch.setattr(stack, "run_command", docker)

    result = CliRunner().invoke(cli_app.app, ["deploy", TAG])

    assert result.exit_code == 0, result.output
    assert {key: _env(configured)[key] for key in PULLED} == PULLED
    assert docker.ups()


def test_the_deploy_command_refuses_an_unconfigured_checkout(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker()
    monkeypatch.setattr(stack, "run_command", docker)

    result = CliRunner().invoke(cli_app.app, ["deploy", TAG])

    assert result.exit_code == 1
    assert "not configured" in result.output
    assert docker.calls == []
    assert not (checkout / envfiles.ENV_FILES["env"]).exists()


@pytest.mark.requires_docker_compose
def test_compose_runs_web_and_migrate_on_the_deployed_web_image(configured: Path) -> None:
    """Both services run the image the deploy names, and without one each
    checkout keeps its own project-scoped web image."""
    shutil.copy(REPO_ROOT / stack.COMPOSE_FILE, configured / stack.COMPOSE_FILE)

    def images() -> dict[str, str]:
        result = subprocess.run(
            (*COMPOSE, "config", "--format", "json"),
            cwd=configured,
            env=envfiles.docker_env(configured),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        config = json.loads(result.stdout)
        services = config["services"]
        return {
            "project": config["name"],
            "web": services[stack.WEB_SERVICE]["image"],
            "migrate": services["migrate"]["image"],
        }

    local = images()
    assert local["web"] == local["migrate"] == f"{local['project']}-openscientist"
    envfiles.set_file_value(
        configured, "env", "OPENSCIENTIST_WEB_IMAGE", PULLED["OPENSCIENTIST_WEB_IMAGE"]
    )
    deployed = images()
    assert deployed["web"] == deployed["migrate"] == PULLED["OPENSCIENTIST_WEB_IMAGE"]

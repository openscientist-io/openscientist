"""Docker and compose read the env files as the CLI just wrote them.

Compose prefers a variable from the calling environment over .env when it
fills the compose file, so the CLI hands docker an environment without any key
its env files define. A value the run writes, the image digests first, then
reaches the containers instead of the copy the environment held.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles, stack
from tests.cli.conftest import REPO_ROOT, env_value

runner = CliRunner()

OLD_AGENT = "sha256:" + "0" * 64
OLD_EXECUTOR = "sha256:" + "1" * 64
NEW_AGENT = "sha256:" + "a" * 64
NEW_EXECUTOR = "sha256:" + "b" * 64


def _configure(checkout: Path) -> None:
    """A configured checkout whose .env holds the digests of the previous build."""
    for name in ("env", "db", "owner"):
        path = checkout / envfiles.ENV_FILES[name]
        path.write_text(
            (checkout / envfiles.EXAMPLE_FILES[name]).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    for key, value in {
        "OPENSCIENTIST_PROVIDER": "anthropic",
        "POSTGRES_WEB_PASSWORD": "web-password",
        "POSTGRES_ADMIN_PASSWORD": "admin-password",
        "OPENSCIENTIST_SECRET_KEY": "c" * 64,
        "LAUNCHER_INSTANCE_ID": "e2e0launcher",
        "OPENSCIENTIST_HOST_PROJECT_DIR": str(checkout),
        "AGENT_IMAGE_DIGESTS": OLD_AGENT,
        "EXECUTOR_IMAGE_DIGESTS": OLD_EXECUTOR,
    }.items():
        envfiles.set_file_value(checkout, "env", key, value)
    envfiles.set_file_value(checkout, "db", "POSTGRES_PASSWORD", "superuser-password")
    envfiles.set_file_value(checkout, "owner", "POSTGRES_OWNER_PASSWORD", "owner-password")
    assert envfiles.is_configured(checkout)


def _stale_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process environment holds copies of keys from all three files, as
    it did when an import loaded .env into it."""
    monkeypatch.setenv("AGENT_IMAGE_DIGESTS", OLD_AGENT)
    monkeypatch.setenv("EXECUTOR_IMAGE_DIGESTS", OLD_EXECUTOR)
    monkeypatch.setenv("OPENSCIENTIST_HOST_PROJECT_DIR", "/a/stale/checkout")
    monkeypatch.setenv("POSTGRES_PASSWORD", "stale-superuser-password")
    monkeypatch.setenv("POSTGRES_OWNER_PASSWORD", "stale-owner-password")
    monkeypatch.setenv("OPENSCIENTIST_TEST_UNRELATED", "kept")


_FILE_KEYS = (
    "AGENT_IMAGE_DIGESTS",
    "EXECUTOR_IMAGE_DIGESTS",
    "OPENSCIENTIST_HOST_PROJECT_DIR",
    "POSTGRES_PASSWORD",
    "POSTGRES_OWNER_PASSWORD",
)


def test_compose_up_gets_no_copy_of_a_key_the_env_files_define(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run records new digests after the builds, and every docker and
    compose call, `up` included, leaves the env files' keys to the files."""
    _configure(checkout)
    _stale_environment(monkeypatch)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        calls.append((argv, dict(env)))
        return 0

    monkeypatch.setattr(stack, "run_command", run)
    monkeypatch.setattr(
        stack, "_image_id", lambda image: NEW_AGENT if "agent" in image else NEW_EXECUTOR
    )
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)

    result = runner.invoke(cli_app.app, [])

    assert result.exit_code == 0, result.output
    assert env_value(checkout, ".env", "AGENT_IMAGE_DIGESTS") == NEW_AGENT
    assert env_value(checkout, ".env", "EXECUTOR_IMAGE_DIGESTS") == NEW_EXECUTOR
    [up_env] = [env for argv, env in calls if "up" in argv]
    for argv, env in calls:
        assert not set(_FILE_KEYS) & env.keys(), argv
        assert env["OPENSCIENTIST_TEST_UNRELATED"] == "kept"
    assert up_env["PATH"]
    builds = [env for argv, env in calls if "build" in argv]
    assert builds
    assert all(env["DOCKER_DEFAULT_PLATFORM"] == stack.BUILD_PLATFORM for env in builds)


def test_stop_gets_no_copy_of_a_key_the_env_files_define(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(checkout)
    _stale_environment(monkeypatch)
    calls: list[dict[str, str]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str]) -> int:
        calls.append(dict(env))
        return 0

    monkeypatch.setattr(stack, "run_command", run)
    result = runner.invoke(cli_app.app, ["stop"])

    assert result.exit_code == 0, result.output
    [env] = calls
    assert not set(_FILE_KEYS) & env.keys()


@pytest.mark.requires_docker_compose
def test_compose_fills_the_launcher_with_the_digests_the_run_wrote(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real compose fills the launcher's allow-list from the .env the run
    rewrote, while the stale copy in the process environment would have won."""
    _configure(checkout)
    shutil.copy(REPO_ROOT / stack.COMPOSE_FILE, checkout / stack.COMPOSE_FILE)
    _stale_environment(monkeypatch)
    envfiles.set_file_value(checkout, "env", "AGENT_IMAGE_DIGESTS", NEW_AGENT)
    envfiles.set_file_value(checkout, "env", "EXECUTOR_IMAGE_DIGESTS", NEW_EXECUTOR)

    def launcher_environment(env: Mapping[str, str]) -> dict[str, str]:
        result = subprocess.run(
            ("docker", "compose", "-f", stack.COMPOSE_FILE, "config", "--format", "json"),
            cwd=checkout,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        launcher: dict[str, str] = json.loads(result.stdout)["services"]["launcher"]["environment"]
        return launcher

    stale = launcher_environment(os.environ)
    assert stale["AGENT_IMAGE_DIGESTS"] == OLD_AGENT, "the stale copy no longer shadows .env"

    launcher = launcher_environment(envfiles.docker_env(checkout))
    assert launcher["AGENT_IMAGE_DIGESTS"] == NEW_AGENT
    assert launcher["EXECUTOR_IMAGE_DIGESTS"] == NEW_EXECUTOR
    assert launcher["JOBS_ROOT"] == f"{checkout}/jobs"

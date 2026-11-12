"""The launcher in a container of its own, as compose runs it.

Docker resolves every bind source on the host, so the jobs directory the
launcher reads must sit at its host path inside the launcher's container too.
These tests run the launcher module in a container, with the jobs directory
mounted at a container path that differs from its host path and at its own
host path.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from docker.models.containers import Container

import docker
from docker import errors as docker_errors
from openscientist_launcher.host_paths import jobs_root_problem
from tests.launcher import LAUNCHER_INSTANCE_ID, REPO_ROOT, VENV_PYTHON, image_digest
from tests.test_launcher_api import JOB_ID, _agent_body, _build_test_agent_image
from tests.test_launcher_validation import _config

_LAUNCHER_NAME = f"openscientist-launcher-mount-test-{os.getpid()}"

#: The base image's pinned Python, whose Debian trixie glibc is new enough for
#: both uv's interpreter and the CI runner's, which this test mounts and runs.
_LAUNCHER_RUNTIME_IMAGE = (
    "python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"
)


@pytest.fixture(scope="module")
def docker_client() -> Iterator[docker.DockerClient]:
    try:
        client = docker.from_env()
        client.ping()
    except docker_errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")
    yield client
    client.close()


@pytest.fixture(scope="module")
def agent_image(docker_client: docker.DockerClient) -> str:
    return _build_test_agent_image(docker_client)


@pytest.fixture()
def host_jobs(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs"
    (jobs / JOB_ID / "data").mkdir(parents=True)
    (jobs / JOB_ID).chmod(0o777)
    (jobs / JOB_ID / "marker.txt").write_text("the host job directory", encoding="utf-8")
    return jobs


def _run_launcher(
    client: docker.DockerClient,
    agent_image: str,
    *,
    host_jobs: Path,
    container_jobs: str,
    socket_dir: Path,
) -> Container:
    """The launcher module in a plain Python userland, with this checkout's
    interpreter and sources mounted read-only at their own paths."""
    digest = image_digest(client, agent_image)
    env = {
        "JOBS_ROOT": container_jobs,
        "AGENT_IMAGE": agent_image,
        "EXECUTOR_IMAGE": agent_image,
        "AGENT_IMAGE_DIGESTS": digest,
        "EXECUTOR_IMAGE_DIGESTS": digest,
        "LAUNCHER_INSTANCE_ID": LAUNCHER_INSTANCE_ID,
        "LAUNCHER_WEB_UID": str(os.getuid()),
        "LAUNCHER_WEB_GID": str(os.getgid()),
        "LAUNCHER_SOCKET_PATH": str(socket_dir / "launcher.sock"),
        "LAUNCHER_NETWORK": "bridge",
        "AGENT_MAX_MEMORY_MIB": "8192",
        "AGENT_MAX_CPU": "4",
        "EXECUTOR_MAX_MEMORY_MIB": "2048",
        "EXECUTOR_MAX_CPU": "1",
        "MAX_LIFETIME_SECONDS": "3600",
    }
    interpreter_home = Path(sys.base_prefix)
    return client.containers.run(
        _LAUNCHER_RUNTIME_IMAGE,
        [str(VENV_PYTHON), "-m", "openscientist_launcher"],
        name=_LAUNCHER_NAME,
        user="root",
        entrypoint=[],
        working_dir=str(REPO_ROOT),
        environment=env,
        detach=True,
        volumes={
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            str(REPO_ROOT): {"bind": str(REPO_ROOT), "mode": "ro"},
            str(interpreter_home): {"bind": str(interpreter_home), "mode": "ro"},
            str(host_jobs): {"bind": container_jobs, "mode": "ro"},
            str(socket_dir): {"bind": str(socket_dir), "mode": "rw"},
        },
    )


@pytest.fixture()
def launcher_container(docker_client: docker.DockerClient) -> Iterator[list[Container]]:
    started: list[Container] = []
    yield started
    for container in started:
        container.remove(force=True)
    for stale in docker_client.containers.list(
        all=True, filters={"name": f"openscientist-agent-{JOB_ID[:12]}"}
    ):
        stale.remove(force=True)


def _exit_code_within(container: Container, seconds: float) -> int | None:
    """The container's exit code once it stops, or None while it still runs at the deadline."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "exited":
            return int(container.attrs["State"]["ExitCode"])
        time.sleep(0.5)
    return None


def _wait_healthy(socket_path: Path, container: Container) -> httpx.Client:
    client = httpx.Client(
        transport=httpx.HTTPTransport(uds=str(socket_path)), base_url="http://launcher", timeout=120
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if client.get("/v1/health").status_code == 200:
                return client
        except httpx.HTTPError:
            container.reload()
            if container.status == "exited":
                break
            time.sleep(0.5)
    pytest.fail(f"the launcher never became healthy:\n{container.logs().decode(errors='replace')}")


def test_a_jobs_mount_at_another_container_path_stops_the_launcher(
    docker_client: docker.DockerClient,
    agent_image: str,
    host_jobs: Path,
    tmp_path: Path,
    launcher_container: list[Container],
) -> None:
    """Mounted at /jobs, the jobs directory would make every bind source a path
    that does not exist on the host, so the launcher refuses to start."""
    socket_dir = tmp_path / "run"
    socket_dir.mkdir()
    container = _run_launcher(
        docker_client,
        agent_image,
        host_jobs=host_jobs,
        container_jobs="/jobs",
        socket_dir=socket_dir,
    )
    launcher_container.append(container)
    exit_code = _exit_code_within(container, 30)
    logs = container.logs().decode(errors="replace")
    assert exit_code not in (None, 0), logs
    assert str(host_jobs) in logs
    assert not (socket_dir / "launcher.sock").exists()


def test_a_jobs_mount_at_its_host_path_gives_the_agent_the_job_directory(
    docker_client: docker.DockerClient,
    agent_image: str,
    host_jobs: Path,
    tmp_path: Path,
    launcher_container: list[Container],
) -> None:
    """Mounted at its own host path, the jobs directory reaches the agent: its
    job mount holds the files the web wrote."""
    socket_dir = tmp_path / "run"
    socket_dir.mkdir()
    container = _run_launcher(
        docker_client,
        agent_image,
        host_jobs=host_jobs,
        container_jobs=str(host_jobs),
        socket_dir=socket_dir,
    )
    launcher_container.append(container)
    client = _wait_healthy(socket_dir / "launcher.sock", container)
    try:
        response = client.post("/v1/containers/agent", json=_agent_body())
    finally:
        client.close()
    assert response.status_code == 200, response.text
    agent = docker_client.containers.get(response.json()["id"])
    result = agent.exec_run(["cat", f"/agent/jobs/{JOB_ID}/marker.txt"], user="root")
    assert result.exit_code == 0, result.output
    assert result.output.decode() == "the host job directory"


def test_a_parent_directory_mounted_at_its_host_path_covers_the_jobs_root(tmp_path: Path) -> None:
    jobs = tmp_path / "checkout" / "jobs"
    jobs.mkdir(parents=True)
    checkout = str(tmp_path / "checkout")
    mounts = [{"Type": "bind", "Source": checkout, "Destination": checkout}]
    assert jobs_root_problem(_config(jobs), mounts) is None


def test_a_jobs_root_on_a_named_volume_is_refused(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    mounts = [
        {"Type": "volume", "Source": "/var/lib/docker/volumes/j/_data", "Destination": str(jobs)}
    ]
    problem = jobs_root_problem(_config(jobs), mounts)
    assert problem is not None
    assert "volume" in problem

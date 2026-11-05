"""Launcher API tests against a real Docker daemon and the real launcher process.

Each test starts the launcher as a subprocess on a unix socket (the same code
path as compose) and talks to it with httpx over that socket. The agent template
tests run against a slim stand-in image that sleeps; the executor tests run
against the real executor image.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
import pytest
from docker.models.containers import Container

import docker
from openscientist_launcher.config import LauncherConfig
from openscientist_launcher.templates import executor_run_kwargs
from openscientist_launcher.validation import validate_exec_request
from tests.launcher import (
    EXECUTOR_IMAGE,
    LAUNCHER_INSTANCE_ID,
    VENV_PYTHON,
    Launcher,
    image_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_IMAGE = "openscientist-launcher-test:latest"
JOB_ID = "11111111-2222-3333-4444-555555555555"
LOGIN_ROLE = "agent_" + "a" * 32
INSTANCE_ID = LAUNCHER_INSTANCE_ID


def _build_test_agent_image(client: docker.DockerClient) -> str:
    """Build the slim agent stand-in image from the files in the tree."""
    import tempfile

    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.launcher-test").read_text(encoding="utf-8")
    entrypoint = (REPO_ROOT / "docker" / "agent-firewall-entrypoint.sh").read_bytes()
    tar_path = Path(tempfile.mkstemp(suffix=".tar", dir=REPO_ROOT / "docker")[1])
    try:
        with tarfile.open(tar_path, "w") as tar:
            for name, data in (
                ("Dockerfile", dockerfile.encode()),
                ("docker/agent-firewall-entrypoint.sh", entrypoint),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with tar_path.open("rb") as tar_file:
            client.images.build(fileobj=tar_file, tag=TEST_IMAGE, rm=True, custom_context=True)
    finally:
        tar_path.unlink(missing_ok=True)
    return TEST_IMAGE


@pytest.fixture(scope="module")
def docker_client() -> Iterator[docker.DockerClient]:
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")
    yield client


@pytest.fixture(scope="module")
def test_agent_image(docker_client: docker.DockerClient) -> str:
    return _build_test_agent_image(docker_client)


@pytest.fixture()
def jobs_root(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs"
    (jobs / JOB_ID).mkdir(parents=True)
    return jobs


@pytest.fixture(autouse=True)
def _clear_stale_agent(docker_client: docker.DockerClient) -> Iterator[None]:
    # The agent name is deterministic per job, so a crashed earlier run can
    # hold the name; the web would remove its own agent before relaunching.
    for container in docker_client.containers.list(
        all=True, filters={"name": f"openscientist-agent-{JOB_ID[:12]}"}
    ):
        container.remove(force=True)
    yield


@pytest.fixture(autouse=True)
def _require_executor_image(docker_client: docker.DockerClient) -> None:
    try:
        docker_client.images.get(EXECUTOR_IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip("Executor image not built. Run 'uv run openscientist' first.")


def _agent_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "job_id": JOB_ID,
        "mode": "discovery",
        "lifetime_seconds": 600,
        "env": {
            "JOB_ID": JOB_ID,
            "JOB_DIR": f"/agent/jobs/{JOB_ID}",
            "DATABASE_URL": "postgresql://w:p@db:5432/os",
        },
        "db_login": LOGIN_ROLE,
        "egress_allow": ["openscientist:8081", "openscientist:8082"],
        "memory_mib": 4096,
        "cpu": 2.0,
    }
    chat_turn = overrides.pop("chat_turn", None)
    body.update(overrides)
    if chat_turn is not None:
        body["chat_turn"] = str(chat_turn)
    return body


@pytest.fixture()
def launcher(
    docker_client: docker.DockerClient, test_agent_image: str, jobs_root: Path
) -> Iterator[Launcher]:
    env = {
        "AGENT_IMAGE": TEST_IMAGE,
        "AGENT_IMAGE_DIGESTS": image_digest(docker_client, test_agent_image),
        "EXECUTOR_IMAGE_DIGESTS": image_digest(docker_client, EXECUTOR_IMAGE),
    }
    started = Launcher.start(
        jobs_root,
        jobs_root,
        AGENT_IMAGE_DIGESTS=env["AGENT_IMAGE_DIGESTS"],
        EXECUTOR_IMAGE_DIGESTS=env["EXECUTOR_IMAGE_DIGESTS"],
        AGENT_IMAGE=env["AGENT_IMAGE"],
        MAX_EGRESS_ENTRIES="8",
    )
    try:
        yield started
    finally:
        started.close()


def _inspect(docker_client: docker.DockerClient, name: str) -> dict[str, Any]:
    # Docker inspect output is untyped at depth; the tests index it freely.
    return docker_client.containers.get(name).attrs


def _host_binds(host_config: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """(source, target, read-only) of each mount, all of them bind mounts, which
    the daemon refuses for a source missing on the host."""
    mounts = host_config.get("Mounts") or []
    assert all(mount["Type"] == "bind" for mount in mounts)
    return [(mount["Source"], mount["Target"], bool(mount.get("ReadOnly"))) for mount in mounts]


class TestHealth:
    def test_health_reports_daemon_and_images(self, launcher: Launcher) -> None:
        with launcher.client() as client:
            response = client.get("/v1/health")
        assert response.status_code == 200
        assert response.json() == {"docker": True, "images": {"agent": True, "executor": True}}


class TestAgentCreate:
    def test_valid_request_creates_the_template_container(
        self, launcher: Launcher, docker_client: docker.DockerClient, jobs_root: Path
    ) -> None:
        with launcher.client() as client:
            response = client.post("/v1/containers/agent", json=_agent_body())
        assert response.status_code == 200, response.text
        body = response.json()
        name = f"openscientist-agent-{JOB_ID[:12]}"
        assert body["name"] == name
        assert body["id"] and body["short_id"]
        assert body["status"] in {"created", "running"}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _inspect(docker_client, name)["State"]["Status"] == "running":
                break
            time.sleep(0.05)
        else:
            pytest.fail("agent container never reached running")

        attrs = _inspect(docker_client, name)
        config = attrs["Config"]
        host = attrs["HostConfig"]
        # The open agent starts behind the restrict firewall, so the web sees
        # root and NET_ADMIN at creation, and the entrypoint drops to the
        # agent user.
        assert config["User"] == "root"
        assert config["Entrypoint"] == ["/agent-firewall-entrypoint.sh"]
        assert host["Privileged"] is False
        assert host["CapAdd"] == ["NET_ADMIN"]
        assert host.get("Binds") is None
        assert _host_binds(host) == [(str(jobs_root / JOB_ID), f"/agent/jobs/{JOB_ID}", False)]
        assert host["NetworkMode"] == "bridge"
        assert host["SecurityOpt"] == ["no-new-privileges:true"]
        assert host["Memory"] == 4096 * 1024**2
        assert host["NanoCpus"] == 2_000_000_000
        assert host["ExtraHosts"] == ["host.docker.internal:host-gateway"]
        assert config["Labels"] == {
            "openscientist.job_id": JOB_ID,
            "openscientist.type": "agent",
            "openscientist.db_login": LOGIN_ROLE,
            "openscientist.launcher": INSTANCE_ID,
        }
        env = dict(item.split("=", 1) for item in config["Env"] if "=" in item)
        assert env["DATABASE_URL"] == "postgresql://w:p@db:5432/os"
        assert env["OPENSCIENTIST_FIREWALL_ALLOW"] == "openscientist:8081,openscientist:8082"
        assert env["OPENSCIENTIST_FIREWALL_MODE"] == "restrict"

        with launcher.client() as client:
            assert client.get(f"/v1/containers/{name}").json() == {
                "status": "running",
                "exit_code": None,
            }
            assert client.post(f"/v1/containers/{name}/stop", json={}).status_code == 200
            assert client.get(f"/v1/containers/{name}").json()["status"] == "exited"
            assert client.get(f"/v1/containers/{name}/logs").json()["logs"] == ""
            assert client.post(f"/v1/containers/{name}/remove").status_code == 200
            assert client.get(f"/v1/containers/{name}").status_code == 404

    def test_chat_mode_names_and_labels(
        self, launcher: Launcher, docker_client: docker.DockerClient
    ) -> None:
        with launcher.client() as client:
            response = client.post(
                "/v1/containers/agent", json=_agent_body(mode="chat", chat_turn="hello there")
            )
        assert response.status_code == 200, response.text
        name = response.json()["name"]
        assert name.startswith(f"openscientist-chat-{JOB_ID[:12]}")
        attrs = _inspect(docker_client, name)
        assert attrs["Config"]["Labels"]["openscientist.type"] == "chat"
        env = dict(item.split("=", 1) for item in attrs["Config"]["Env"] if "=" in item)
        assert env["OPENSCIENTIST_CHAT_TURN"] == "hello there"
        with launcher.client() as client:
            client.post(f"/v1/containers/{name}/remove")

    def test_airgapped_firewall_trio(
        self, launcher: Launcher, docker_client: docker.DockerClient
    ) -> None:
        body = _agent_body(airgapped=True, egress_allow=["postgres:5432", "api.openai.com:443"])
        with launcher.client() as client:
            response = client.post("/v1/containers/agent", json=body)
        assert response.status_code == 200, response.text
        attrs = _inspect(docker_client, response.json()["name"])
        assert attrs["HostConfig"]["CapAdd"] == ["NET_ADMIN"]
        assert attrs["Config"]["User"] == "root"
        assert attrs["Config"]["Entrypoint"] == ["/agent-firewall-entrypoint.sh"]
        env = dict(item.split("=", 1) for item in attrs["Config"]["Env"] if "=" in item)
        assert env["OPENSCIENTIST_FIREWALL_ALLOW"] == "postgres:5432,api.openai.com:443"
        assert env["OPENSCIENTIST_FIREWALL_MODE"] == "airgap"
        with launcher.client() as client:
            client.post(f"/v1/containers/{response.json()['name']}/remove")

    @pytest.mark.parametrize(
        "field", ["privileged", "cap_add", "volumes", "network", "user", "image"]
    )
    def test_injection_fields_refused(self, launcher: Launcher, field: str) -> None:
        with launcher.client() as client:
            response = client.post("/v1/containers/agent", json=_agent_body(**{field: "injected"}))
        assert response.status_code == 400, response.text
        assert response.json()["error"].startswith("bad_request")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"run_home": {"path": "/tmp/outside-jobs"}},
            {"phenix": True},
        ],
        ids=["run-home-outside", "phenix-unconfigured"],
    )
    def test_host_mounts_refused(self, launcher: Launcher, overrides: dict[str, object]) -> None:
        with launcher.client() as client:
            response = client.post("/v1/containers/agent", json=_agent_body(**overrides))
        assert response.status_code == 400, response.text
        assert response.json()["error"].startswith("bad_request")

    def test_gcp_credentials_never_mounted_into_an_agent(
        self,
        docker_client: docker.DockerClient,
        test_agent_image: str,
        jobs_root: Path,
        tmp_path: Path,
    ) -> None:
        """An operator's GCP service account never enters an agent container."""
        creds = tmp_path / "service-account.json"
        creds.write_text("{}", encoding="utf-8")
        started = Launcher.start(
            jobs_root,
            jobs_root,
            AGENT_IMAGE=TEST_IMAGE,
            AGENT_IMAGE_DIGESTS=image_digest(docker_client, test_agent_image),
            EXECUTOR_IMAGE_DIGESTS=image_digest(docker_client, EXECUTOR_IMAGE),
            GCP_CREDENTIALS_HOST_PATH=str(creds),
        )
        try:
            with started.client() as client:
                response = client.post(
                    "/v1/containers/agent", json=_agent_body(gcp_credentials=True)
                )
        finally:
            started.close()
        assert response.status_code == 400, response.text
        assert response.json()["error"].startswith("bad_request")
        assert not docker_client.containers.list(
            all=True, filters={"name": f"openscientist-agent-{JOB_ID[:12]}"}
        )

    def test_a_bind_source_missing_on_the_host_is_refused_and_never_created(
        self,
        docker_client: docker.DockerClient,
        test_agent_image: str,
        jobs_root: Path,
        tmp_path: Path,
    ) -> None:
        """Docker refuses the launch rather than creating the missing source as
        an empty root-owned directory."""
        missing = tmp_path / "no-phenix-here"
        started = Launcher.start(
            jobs_root,
            jobs_root,
            AGENT_IMAGE=TEST_IMAGE,
            AGENT_IMAGE_DIGESTS=image_digest(docker_client, test_agent_image),
            EXECUTOR_IMAGE_DIGESTS=image_digest(docker_client, EXECUTOR_IMAGE),
            PHENIX_HOST_PATH=str(missing),
        )
        try:
            with started.client() as client:
                response = client.post("/v1/containers/agent", json=_agent_body(phenix=True))
        finally:
            started.close()
        assert response.status_code == 502, response.text
        assert response.json()["error"].startswith("docker_error")
        assert not missing.exists()
        assert not docker_client.containers.list(
            all=True, filters={"name": f"openscientist-agent-{JOB_ID[:12]}"}
        )

    @pytest.mark.parametrize("entry", ["db :5432", "db\n:5432", "nft add table x", "db:5432:extra"])
    def test_bad_egress_entry_refused(self, launcher: Launcher, entry: str) -> None:
        with launcher.client() as client:
            response = client.post(
                "/v1/containers/agent",
                json=_agent_body(airgapped=True, egress_allow=[entry]),
            )
        assert response.status_code == 400, response.text
        assert response.json()["error"].startswith("bad_request")

    def test_resource_values_above_bounds_refused(self, launcher: Launcher) -> None:
        with launcher.client() as client:
            memory = client.post("/v1/containers/agent", json=_agent_body(memory_mib=8193))
            cpu = client.post("/v1/containers/agent", json=_agent_body(cpu=4.1))
            lifetime = client.post("/v1/containers/agent", json=_agent_body(lifetime_seconds=3601))
        for response in (memory, cpu, lifetime):
            assert response.status_code == 400, response.text
            assert response.json()["error"].startswith("bad_request")

    def test_resource_values_at_the_bound_apply(
        self, launcher: Launcher, docker_client: docker.DockerClient
    ) -> None:
        with launcher.client() as client:
            response = client.post(
                "/v1/containers/agent",
                json=_agent_body(memory_mib=8192, cpu=4.0, lifetime_seconds=3600),
            )
        assert response.status_code == 200, response.text
        attrs = _inspect(docker_client, response.json()["name"])
        assert attrs["HostConfig"]["Memory"] == 8192 * 1024**2
        assert attrs["HostConfig"]["NanoCpus"] == 4_000_000_000
        with launcher.client() as client:
            client.post(f"/v1/containers/{response.json()['name']}/remove")


class TestRunHome:
    """The run home mounts outside the job mount, so the daemon's mountpoint
    never lands in the host job directory."""

    def test_the_run_home_mount_stays_out_of_the_job_directory(
        self,
        launcher: Launcher,
        docker_client: docker.DockerClient,
        jobs_root: Path,
    ) -> None:
        home = jobs_root / ".run-homes" / LOGIN_ROLE
        home.mkdir(parents=True)
        try:
            os.chown(home, -1, 1001)
        except PermissionError:
            pytest.skip("cannot give the test run home the jobs group")
        home.chmod(0o770)
        (jobs_root / JOB_ID / "keep").write_text("keep", encoding="utf-8")
        with launcher.client() as client:
            response = client.post(
                "/v1/containers/agent", json=_agent_body(run_home={"path": str(home)})
            )
        assert response.status_code == 200, response.text
        name = response.json()["name"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _inspect(docker_client, name)["State"]["Status"] == "running":
                break
            time.sleep(0.05)
        else:
            pytest.fail("agent container never reached running")
        try:
            job_dir = jobs_root / JOB_ID
            assert sorted(os.listdir(job_dir)) == ["keep"], sorted(os.listdir(job_dir))
            attrs = _inspect(docker_client, name)
            binds = _host_binds(attrs["HostConfig"])
            assert (str(home), "/agent/run-home", False) in binds
            assert not any(target.startswith(f"/agent/jobs/{JOB_ID}/") for _, target, _ in binds)
            env = dict(item.split("=", 1) for item in attrs["Config"]["Env"] if "=" in item)
            assert env["OPENSCIENTIST_RUN_HOME"] == "/agent/run-home"
            result = docker_client.containers.get(name).exec_run(
                ["touch", "/agent/run-home/probe"], user="1001:1001"
            )
            assert result.exit_code == 0, result.output
            assert (home / "probe").stat().st_gid == 1001
        finally:
            with launcher.client() as client:
                client.post(f"/v1/containers/{name}/remove")


class TestImageRefusals:
    def test_missing_image_refused(
        self, docker_client: docker.DockerClient, jobs_root: Path
    ) -> None:
        launcher = Launcher.start(
            jobs_root,
            jobs_root,
            AGENT_IMAGE="no-such-image:nope",
            AGENT_IMAGE_DIGESTS=image_digest(docker_client, TEST_IMAGE),
            EXECUTOR_IMAGE_DIGESTS=image_digest(docker_client, EXECUTOR_IMAGE),
        )
        try:
            with launcher.client() as client:
                health = client.get("/v1/health")
                create = client.post("/v1/containers/agent", json=_agent_body())
            assert health.json() == {
                "docker": True,
                "images": {"agent": False, "executor": True},
            }
            assert create.status_code == 409, create.text
            assert create.json()["error"].startswith("image_missing")
        finally:
            launcher.close()

    def test_unknown_image_digest_refused(
        self, docker_client: docker.DockerClient, jobs_root: Path
    ) -> None:
        launcher = Launcher.start(
            jobs_root,
            jobs_root,
            AGENT_IMAGE_DIGESTS="sha256:" + "0" * 64,
            EXECUTOR_IMAGE_DIGESTS=image_digest(docker_client, EXECUTOR_IMAGE),
        )
        try:
            with launcher.client() as client:
                health = client.get("/v1/health")
                create = client.post("/v1/containers/agent", json=_agent_body())
            assert health.json()["images"] == {"agent": False, "executor": True}
            assert create.status_code == 409, create.text
            assert create.json()["error"].startswith("image_missing")
        finally:
            launcher.close()


class TestForeignContainers:
    def test_foreign_ids_get_uniform_404(
        self, launcher: Launcher, docker_client: docker.DockerClient
    ) -> None:
        foreign: list[Container] = [
            docker_client.containers.run(
                "hello-world",
                detach=True,
                labels={"openscientist.type": "agent", "openscientist.job_id": JOB_ID},
            ),
            docker_client.containers.run(
                "hello-world",
                detach=True,
                labels={
                    "openscientist.type": "agent",
                    "openscientist.job_id": JOB_ID,
                    "openscientist.launcher": "other-instance",
                },
            ),
        ]
        try:
            with launcher.client() as client:
                create = client.post("/v1/containers/agent", json=_agent_body())
                assert create.status_code == 200, create.text
                listing = client.get(
                    "/v1/containers", params={"label": f"openscientist.job_id={JOB_ID}"}
                )
                assert listing.status_code == 200
                listed = [entry["id"] for entry in listing.json()]
                assert create.json()["id"] in listed
                for container in foreign:
                    assert container.id not in listed

                for ref in (container.id for container in foreign):
                    assert client.post(f"/v1/containers/{ref}/stop").status_code == 404
                    assert client.post(f"/v1/containers/{ref}/remove").status_code == 404
                    assert client.get(f"/v1/containers/{ref}").status_code == 404
                    assert client.get(f"/v1/containers/{ref}/logs").status_code == 404
                    assert (
                        client.post(
                            f"/v1/containers/{ref}/wait", json={"timeout_seconds": 1}
                        ).status_code
                        == 404
                    )
                    assert client.get(f"/v1/containers/{ref}/stats").status_code == 404
                    for response in (client.get(f"/v1/containers/{ref}"),):
                        assert response.json()["error"].startswith("not_found")

                client.post(f"/v1/containers/{create.json()['name']}/remove")
        finally:
            for container in foreign:
                container.remove(force=True)

    def test_list_and_container_scoped_endpoints(
        self, launcher: Launcher, docker_client: docker.DockerClient
    ) -> None:
        with launcher.client() as client:
            create = client.post("/v1/containers/agent", json=_agent_body())
            assert create.status_code == 200, create.text
            name = create.json()["name"]
            entry = next(
                item
                for item in client.get(
                    "/v1/containers",
                    params=[
                        ("label", "openscientist.type=agent"),
                        ("label", f"openscientist.job_id={JOB_ID}"),
                    ],
                ).json()
                if item["name"] == name
            )
            assert entry["id"] == create.json()["id"]
            assert entry["status"] == "running"
            assert entry["created"]
            assert set(entry["labels"]) == {
                "openscientist.job_id",
                "openscientist.type",
                "openscientist.db_login",
                "openscientist.launcher",
            }
            stats = client.get(f"/v1/containers/{name}/stats")
            assert stats.status_code == 200
            assert "memory_stats" in stats.json() and "cpu_stats" in stats.json()
            wait = client.post(f"/v1/containers/{name}/wait", json={"timeout_seconds": 1})
            assert wait.json() == {"exit_code": None, "timeout": True}
            client.post(f"/v1/containers/{name}/stop", json={"timeout_seconds": 5})
            waited = client.post(f"/v1/containers/{name}/wait", json={"timeout_seconds": 5})
            # SIGTERM is 143; this host escalates to SIGKILL (137) for every stop.
            assert waited.json()["timeout"] is False
            assert waited.json()["exit_code"] in {143, 137}
            assert client.get(f"/v1/containers/{name}").json()["status"] == "exited"
            client.post(f"/v1/containers/{name}/remove")


class TestPeerCred:
    def test_wrong_uid_is_refused(
        self, docker_client: docker.DockerClient, jobs_root: Path, tmp_path: Path
    ) -> None:
        launcher = Launcher.start(
            tmp_path,
            jobs_root,
            AGENT_IMAGE_DIGESTS=image_digest(docker_client, TEST_IMAGE),
            EXECUTOR_IMAGE_DIGESTS=image_digest(docker_client, EXECUTOR_IMAGE),
            LAUNCHER_WEB_UID=str(os.getuid() + 4096),
            wait_healthy=False,
        )
        launcher.wait_socket()
        try:
            with launcher.client() as client:
                with pytest.raises(httpx.HTTPError):
                    client.get("/v1/health")
        finally:
            launcher.close()

    def test_wrong_gid_is_refused(
        self, docker_client: docker.DockerClient, jobs_root: Path, tmp_path: Path
    ) -> None:
        launcher = Launcher.start(
            tmp_path,
            jobs_root,
            AGENT_IMAGE_DIGESTS=image_digest(docker_client, TEST_IMAGE),
            EXECUTOR_IMAGE_DIGESTS=image_digest(docker_client, EXECUTOR_IMAGE),
            LAUNCHER_WEB_GID=str(os.getgid() + 4096),
            wait_healthy=False,
        )
        launcher.wait_socket()
        try:
            with launcher.client() as client:
                with pytest.raises(httpx.HTTPError):
                    client.get("/v1/health")
        finally:
            launcher.close()


class TestHealthCheck:
    @staticmethod
    def _health(launcher: Launcher) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(VENV_PYTHON), "-m", "openscientist_launcher.health"],
            cwd=REPO_ROOT,
            env=launcher.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_passes_when_the_web_reaches_the_socket(self, launcher: Launcher) -> None:
        result = self._health(launcher)
        assert result.returncode == 0, result.stderr

    def test_fails_when_the_web_cannot_open_the_socket(self, launcher: Launcher) -> None:
        """A socket file the web's uid may not open fails the check, though the file exists."""
        launcher.socket_path.chmod(0)
        result = self._health(launcher)
        assert result.returncode == 1
        assert "cannot use the socket" in result.stderr


class TestExecutor:
    def test_valid_executor_run_roundtrip(
        self, launcher: Launcher, docker_client: docker.DockerClient, jobs_root: Path
    ) -> None:
        job_dir = jobs_root / JOB_ID
        out_dir = job_dir / "out"
        out_dir.mkdir()
        (job_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        code = "import time\ntime.sleep(3)\nprint('executor-alive')"
        body = {
            "job_id": JOB_ID,
            "timeout_seconds": 120,
            "memory_mib": 1024,
            "cpu": 0.5,
            "egress_allow": ["openscientist:8081", "openscientist:8082"],
            "input": {
                "code": code,
                "language": "python",
                "data_path": str(job_dir / "data.csv"),
                "output_dir": str(out_dir),
                "timeout": 60,
                "description": "test",
                "iteration": 0,
                "data_files": [{"path": str(job_dir / "data.csv"), "name": "data.csv"}],
            },
        }
        # The run removes its container on completion, so a second thread
        # inspects the config while it is still alive.
        inspected: dict[str, Any] = {}
        watch_errors: list[str] = []

        def _watch() -> None:
            try:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    # Other test launchers share the daemon, so the instance
                    # marker keeps the watcher on this run's container.
                    containers = docker_client.containers.list(
                        filters={
                            "label": [
                                "openscientist.type=executor",
                                f"openscientist.launcher={LAUNCHER_INSTANCE_ID}",
                                f"openscientist.job_id={JOB_ID}",
                            ]
                        }
                    )
                    if containers:
                        inspected["attrs"] = containers[0].attrs
                        return
                    time.sleep(0.2)
            except Exception as exc:
                watch_errors.append(f"watcher: {exc}")

        watcher = Thread(target=_watch, daemon=True)
        watcher.start()
        with launcher.client() as client:
            response = client.post("/v1/exec", json=body)
        watcher.join(timeout=120)
        assert not watch_errors, watch_errors
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["exit_code"] == 0
        assert "executor-alive" in result["stdout"]
        last_line = [line for line in result["stdout"].splitlines() if line.startswith("{")][-1]
        assert json.loads(last_line)["success"] is True

        attrs = inspected["attrs"]
        config = attrs["Config"]
        host = attrs["HostConfig"]
        # The open-network executor starts behind the restrict firewall, the
        # same launch shape the web's direct path used to build.
        assert config["User"] == "root"
        assert config["Entrypoint"] == ["/agent-firewall-entrypoint.sh"]
        assert config["StopSignal"] == "SIGKILL"
        assert host["SecurityOpt"] == ["no-new-privileges:true"]
        assert host["NetworkMode"] == "bridge"
        assert host["CapAdd"] == ["NET_ADMIN"]
        assert host["Memory"] == 1024 * 1024**2
        assert host["NanoCpus"] == 500_000_000
        assert host.get("Binds") is None
        job_mount = f"/agent/jobs/{JOB_ID}"
        assert _host_binds(host) == [(str(job_dir), job_mount, False)]
        env = dict(item.split("=", 1) for item in config["Env"] if "=" in item)
        assert env["OPENSCIENTIST_FIREWALL_MODE"] == "restrict"
        assert env["OPENSCIENTIST_FIREWALL_RUN_AS"] == "executor"
        assert env["OPENSCIENTIST_FIREWALL_ALLOW"] == "openscientist:8081,openscientist:8082"
        assert config["Labels"]["openscientist.type"] == "executor"
        assert config["Labels"]["openscientist.launcher"] == INSTANCE_ID
        payload = {
            "code": code,
            "language": "python",
            "data_path": f"{job_mount}/data.csv",
            "output_dir": f"{job_mount}/out",
            "timeout": 60,
            "description": "test",
            "iteration": 0,
            "data_files": [{"path": f"{job_mount}/data.csv", "name": "data.csv"}],
        }
        b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        assert config["Cmd"] == [
            "sh",
            "-c",
            f"echo {b64} | base64 -d | python -m openscientist_executor",
        ]
        with launcher.client() as client:
            listing = client.get(
                "/v1/containers", params={"label": f"openscientist.job_id={JOB_ID}"}
            ).json()
        assert not [
            entry for entry in listing if entry["labels"].get("openscientist.type") == "executor"
        ]

    @pytest.mark.parametrize("swapped", ["output", "data"])
    def test_a_path_swapped_for_a_symlink_after_validation_stays_inside_the_job(
        self,
        launcher: Launcher,
        docker_client: docker.DockerClient,
        jobs_root: Path,
        tmp_path: Path,
        swapped: str,
    ) -> None:
        """The agent owns its job directory, so between the launcher's check
        and the executor's mount it can replace a checked directory with a
        symlink to somewhere else on the host. The executor must still see
        only the job's own files."""
        outside = tmp_path / "another-job"
        outside.mkdir()
        (outside / "secret.txt").write_text("another job's data", encoding="utf-8")
        (outside / "rows.csv").write_text("a\n1\n", encoding="utf-8")
        job_dir = jobs_root / JOB_ID
        (job_dir / "out").mkdir()
        (job_dir / "inputs").mkdir()
        (job_dir / "inputs" / "rows.csv").write_text("a\n1\n", encoding="utf-8")
        code = (
            "import os\n"
            "print('listing', sorted(os.listdir('.')))\n"
            "print('listing', sorted(os.listdir(os.path.dirname(data_files[0]['path']))))\n"
        )
        body = {
            "job_id": JOB_ID,
            "timeout_seconds": 120,
            "memory_mib": 1024,
            "cpu": 0.5,
            "egress_allow": ["openscientist:8081", "openscientist:8082"],
            "input": {
                "code": code,
                "language": "python",
                "output_dir": str(job_dir / "out"),
                "timeout": 60,
                "description": "swap",
                "iteration": 0,
                "data_files": [{"path": str(job_dir / "inputs" / "rows.csv"), "name": "rows.csv"}],
            },
        }
        cfg = LauncherConfig.from_env(launcher.env)
        req = validate_exec_request(body, cfg)
        # The agent's move, inside the window the launcher leaves open.
        target = job_dir / ("out" if swapped == "output" else "inputs")
        shutil.rmtree(target)
        target.symlink_to(outside, target_is_directory=True)
        container = docker_client.containers.run(**executor_run_kwargs(cfg, req, network="bridge"))
        try:
            container.wait(timeout=120)
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", "replace")
        finally:
            container.remove(force=True)
        assert "secret.txt" not in logs
        assert sorted(p.name for p in outside.iterdir()) == ["rows.csv", "secret.txt"]

    def test_executor_bounds_and_refusals(
        self,
        launcher: Launcher,
        docker_client: docker.DockerClient,
        jobs_root: Path,
        tmp_path: Path,
    ) -> None:
        job_dir = jobs_root / JOB_ID
        (job_dir / "out").mkdir()

        def _exec(**overrides: object) -> dict[str, object]:
            body: dict[str, object] = {
                "job_id": JOB_ID,
                "timeout_seconds": 60,
                "memory_mib": 1024,
                "cpu": 0.5,
                "egress_allow": ["openscientist:8081", "openscientist:8082"],
                "input": {
                    "code": "print(1)",
                    "language": "python",
                    "data_path": None,
                    "output_dir": str(job_dir / "out"),
                    "timeout": 30,
                    "description": "",
                    "iteration": 0,
                    "data_files": [],
                },
            }
            allow = overrides.pop("egress_allow", "keep")
            body.update(overrides)
            if allow is None:
                body.pop("egress_allow")
            elif allow != "keep":
                body["egress_allow"] = allow
            return body

        with launcher.client() as client:
            memory = client.post("/v1/exec", json=_exec(memory_mib=2049))
            assert memory.status_code == 400
            cpu = client.post("/v1/exec", json=_exec(cpu=1.1))
            assert cpu.status_code == 400
            outside = tmp_path / "out"
            outside.mkdir()
            mount = client.post(
                "/v1/exec",
                json=_exec(
                    **{
                        "input": {
                            "code": "print(1)",
                            "language": "python",
                            "data_path": None,
                            "output_dir": str(outside),
                            "timeout": 30,
                            "description": "",
                            "iteration": 0,
                            "data_files": [],
                        }
                    }
                ),
            )
            assert mount.status_code == 400
            assert mount.json()["error"].startswith("bad_request")
            # The open run needs its allow-list; the air-gapped run takes none.
            missing_allow = client.post("/v1/exec", json=_exec(egress_allow=None))
            assert missing_allow.status_code == 400
            airgapped_with_allow = client.post("/v1/exec", json=_exec(airgapped=True))
            assert airgapped_with_allow.status_code == 400
            bad_entry = client.post("/v1/exec", json=_exec(egress_allow=["nft add table x"]))
            assert bad_entry.status_code == 400
            too_many = client.post(
                "/v1/exec", json=_exec(egress_allow=[f"h:{i}" for i in range(9)])
            )
            assert too_many.status_code == 400
            ok = client.post("/v1/exec", json=_exec())
        assert ok.status_code == 200, ok.text
        assert ok.json()["exit_code"] == 0

    def test_executor_airgapped_uses_no_network(
        self, launcher: Launcher, docker_client: docker.DockerClient, jobs_root: Path
    ) -> None:
        job_dir = jobs_root / JOB_ID
        out_dir = job_dir / "out"
        out_dir.mkdir()
        code = "import time\ntime.sleep(3)"
        body = {
            "job_id": JOB_ID,
            "timeout_seconds": 120,
            "memory_mib": 512,
            "cpu": 0.25,
            "airgapped": True,
            "input": {
                "code": code,
                "language": "python",
                "data_path": None,
                "output_dir": str(out_dir),
                "timeout": 60,
                "description": "",
                "iteration": 0,
                "data_files": [],
            },
        }
        inspected: dict[str, Any] = {}
        watch_errors: list[str] = []

        def _watch() -> None:
            try:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    # Other test launchers share the daemon, so the instance
                    # marker keeps the watcher on this run's container.
                    containers = docker_client.containers.list(
                        filters={
                            "label": [
                                "openscientist.type=executor",
                                f"openscientist.launcher={LAUNCHER_INSTANCE_ID}",
                                f"openscientist.job_id={JOB_ID}",
                            ]
                        }
                    )
                    if containers:
                        inspected["attrs"] = containers[0].attrs
                        return
                    time.sleep(0.2)
            except Exception as exc:
                watch_errors.append(f"watcher: {exc}")

        watcher = Thread(target=_watch, daemon=True)
        watcher.start()
        with launcher.client() as client:
            response = client.post("/v1/exec", json=body)
        watcher.join(timeout=120)
        assert not watch_errors, watch_errors
        assert response.status_code == 200, response.text
        assert response.json()["exit_code"] == 0
        attrs = inspected["attrs"]
        assert attrs["HostConfig"]["NetworkMode"] == "none"
        # With no network there is nothing to filter, so the executor keeps
        # its own user and entrypoint and takes no firewall environment.
        assert attrs["Config"]["User"] == "executor"
        assert not (attrs["HostConfig"].get("CapAdd") or [])
        env = {item.split("=", 1)[0] for item in attrs["Config"]["Env"]}
        assert not any(name.startswith("OPENSCIENTIST_FIREWALL_") for name in env)

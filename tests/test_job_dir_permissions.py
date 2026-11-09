"""Ownership and modes of the job tree the web creates for the job containers.

The web process runs as its own non-root uid in the jobs group and hands every
file it creates in the job tree to the containers' fixed users, stripping the
world bits. Non-root tests observe the chown on a real temp dir; root tests
verify the real ownership.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.job_container.identity import (
    AGENT_GID,
    AGENT_UID,
    EXECUTOR_UID,
    JOB_DIR_MODE,
    JOB_FILE_MODE,
    RUN_HOME_DIR_MODE,
    apply_job_ownership,
)
from tests.launcher import VENV_PYTHON

REPO_ROOT = Path(__file__).resolve().parents[1]
IS_ROOT = os.geteuid() == 0

# Settings refuse master secrets under 64 characters.
TEST_SECRET_KEY = "job-perms-test-secret-" * 3


class ChownRecorder:
    """Observes the chowns the real setup code makes on a real tree.

    Non-root tests force the root code path (which is the one that chowns) and
    let the real chown through, so it succeeds only when the test runs as
    root.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[Path, int, int]] = []
        self._real = os.chown
        monkeypatch.setattr(os, "chown", self._chown)
        if os.geteuid() != 0:
            monkeypatch.setattr(os, "geteuid", lambda: 0)

    def _chown(self, path: Path, uid: int, gid: int) -> None:
        self.calls.append((path, uid, gid))
        with suppress(PermissionError):
            self._real(path, uid, gid)

    def owned(self, path: Path) -> bool:
        if IS_ROOT:
            st = path.stat()
            return (st.st_uid, st.st_gid) == (AGENT_UID, AGENT_GID)
        return any(p == path and (u, g) == (AGENT_UID, AGENT_GID) for p, u, g in self.calls)

    def in_jobs(self, path: Path) -> bool:
        """The path ended up in the jobs group. A setgid parent already
        carries the group, so the helper chowns such paths only when the
        group is still the web's own."""
        return path.stat().st_gid == AGENT_GID or any(
            p == path and g == AGENT_GID for p, _u, g in self.calls
        )


def assert_agent_owned(chowns: ChownRecorder, path: Path) -> None:
    """The path is the agent's. A root run gets that for real, and a
    non-root run holds it when the jobs group already reaches the path
    (the CI runner's primary group is 1001) or the setup requested the
    chown. The ownership itself is covered by the root-in-container
    integration test below."""
    assert chowns.owned(path) or chowns.in_jobs(path), path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestJobSetupOwnership:
    """create_job hands the whole tree to the agent user without world bits."""

    def test_job_tree_is_agent_owned_and_world_closed(
        self, tmp_path: Path, test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.orchestrator.setup import create_job

        chowns = ChownRecorder(monkeypatch)
        job_id = str(uuid4())
        upload = tmp_path / "upload.csv"
        upload.write_text("a,b\n1,2\n", encoding="utf-8")
        job_dir = create_job(job_id, "Question?", [upload], 5, jobs_dir=tmp_path / "jobs")

        assert job_dir.stat().st_mode & stat.S_ISGID, "job dir must be setgid"
        for path in (job_dir, job_dir / "data", job_dir / "provenance", job_dir / ".git"):
            assert _mode(path) == JOB_DIR_MODE, path
            assert chowns.in_jobs(path), path
        for path in (job_dir / "data" / "upload.csv", job_dir / ".git" / "HEAD"):
            assert _mode(path) == JOB_FILE_MODE, path
            assert chowns.in_jobs(path), path

    def test_git_repository_files_are_agent_owned(
        self, tmp_path: Path, test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.orchestrator.setup import create_job

        chowns = ChownRecorder(monkeypatch)
        job_dir = create_job(str(uuid4()), "Question?", [], 5, jobs_dir=tmp_path / "jobs")

        for path in (job_dir / ".git" / "config", job_dir / ".git" / "HEAD"):
            assert _mode(path) == JOB_FILE_MODE, path
            assert chowns.in_jobs(path), path


class TestRunHomeOwnership:
    """Run homes hold operator logins, so root hands them to the agent
    owner-only, and a non-root web opens them to the agent's group instead."""

    def test_run_home_is_agent_owned_and_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.job_container.run_home import create_run_home

        chowns = ChownRecorder(monkeypatch)
        home = create_run_home(tmp_path / "jobs", "agent_" + "1" * 32)

        for path in (home, home.parent):
            assert _mode(path) == RUN_HOME_DIR_MODE, path
            assert_agent_owned(chowns, path)


class TestWebLaterWrites:
    """Files the web writes into a job tree after setup get the same
    ownership and modes."""

    def test_a_web_written_file_in_the_job_tree(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        apply_job_ownership(job_dir)
        request = job_dir / ".chat_request_1.json"
        request.write_text("{}", encoding="utf-8")
        apply_job_ownership(request)

        assert _mode(job_dir) == JOB_DIR_MODE
        assert _mode(request) == JOB_FILE_MODE
        if IS_ROOT:
            # The web keeps ownership; the setgid dir hands it the jobs group.
            assert request.stat().st_uid == 0
            assert request.stat().st_gid == AGENT_GID

    def test_setgid_dir_inherits_the_jobs_group_for_later_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chowns = ChownRecorder(monkeypatch)
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        apply_job_ownership(job_dir)
        assert job_dir.stat().st_mode & stat.S_ISGID

        request = job_dir / ".chat_request_1.json"
        request.write_text("{}", encoding="utf-8")
        apply_job_ownership(request)

        assert _mode(request) == JOB_FILE_MODE
        if IS_ROOT:
            # The setgid parent already hands the file the jobs group, so the
            # helper must not spend a chown on it.
            assert request.stat().st_gid == AGENT_GID
            assert not any(p == request for p, _u, _g in chowns.calls)


class TestDockerfileIdentityDrift:
    """The Dockerfiles pin the same uids the Python code chowns to."""

    @staticmethod
    def _arg(dockerfile: str, name: str) -> int:
        match = re.search(rf"ARG\s+{name}=(\d+)", dockerfile)
        assert match is not None, f"build arg {name} missing"
        return int(match.group(1))

    def test_agent_dockerfile_pins_the_agent_uid_and_gid(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile.agent").read_text(encoding="utf-8")
        assert self._arg(dockerfile, "AGENT_UID") == AGENT_UID
        assert self._arg(dockerfile, "AGENT_GID") == AGENT_GID

    def test_executor_dockerfile_pins_the_executor_uid_and_shared_gid(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile.executor").read_text(encoding="utf-8")
        assert self._arg(dockerfile, "EXECUTOR_UID") == EXECUTOR_UID
        assert self._arg(dockerfile, "JOBS_GID") == AGENT_GID


@pytest.mark.integration
class TestJobTreeAccessInContainers:
    """A job tree set up by the real create_job, seen from the real container
    users. The agent writes, the executor reads and writes its own slice, and
    any other local user is shut out."""

    @pytest.fixture(scope="class")
    def docker_client(self):
        import docker

        client = docker.from_env()
        try:
            client.ping()
        except Exception:
            client.close()
            pytest.skip("Docker not available")

        try:
            yield client
        finally:
            client.close()

    @pytest.fixture(scope="class")
    def agent_image(self, docker_client):
        import docker

        try:
            docker_client.images.get("openscientist-base:latest")
        except docker.errors.ImageNotFound:
            pytest.skip(
                "Base image 'openscientist-base:latest' not built. "
                "Build it before running integration tests."
            )

        tag = f"openscientist-job-perms-test:{uuid4().hex[:12]}"
        try:
            docker_client.images.build(
                path=str(REPO_ROOT),
                dockerfile="Dockerfile.agent",
                tag=tag,
                rm=True,
            )
        except docker.errors.BuildError as exc:
            # build_log is a live stream, so read it before slicing.
            log_tail = "\n".join(
                str(entry.get("stream", "")).rstrip()
                for entry in list(exc.build_log)[-20:]
                if isinstance(entry, dict)
            )
            pytest.fail(f"Failed to build test agent image {tag}:\n{log_tail}")

        try:
            yield tag
        finally:
            with suppress(Exception):
                docker_client.images.remove(tag, force=True)

    @pytest.fixture(scope="class")
    def executor_image(self, docker_client):
        """Built from this worktree rather than a shared tag, because other
        checkouts of this repository rebuild that tag with their own
        Dockerfile."""
        import docker

        try:
            docker_client.images.get("openscientist-base:latest")
        except docker.errors.ImageNotFound:
            pytest.skip(
                "Base image 'openscientist-base:latest' not built. "
                "Build it before running integration tests."
            )

        tag = f"openscientist-executor-perms-test:{uuid4().hex[:12]}"
        try:
            docker_client.images.build(
                path=str(REPO_ROOT),
                dockerfile="Dockerfile.executor",
                tag=tag,
                rm=True,
            )
        except docker.errors.BuildError as exc:
            # build_log is a live stream, so read it before slicing.
            log_tail = "\n".join(
                str(entry.get("stream", "")).rstrip()
                for entry in list(exc.build_log)[-20:]
                if isinstance(entry, dict)
            )
            pytest.fail(f"Failed to build test executor image {tag}:\n{log_tail}")

        try:
            yield tag
        finally:
            with suppress(Exception):
                docker_client.images.remove(tag, force=True)

    @pytest.fixture(scope="class")
    def job_tree(
        self,
        docker_client,
        agent_image,
        test_database_url: str,
        _apply_migrations_once: None,
        tmp_path_factory: pytest.TempPathFactory,
    ):
        """Create one job with the real setup function, run as root inside a
        container so the chown to the agent user actually happens."""
        from tests.db_logins import ADMIN_PASSWORD, login_url

        jobs_dir = tmp_path_factory.mktemp("jobs")
        # The mount parent stands in for the web's jobs root, which any
        # process on the host can traverse.
        jobs_dir.chmod(0o755)
        admin_url = login_url(test_database_url, "openscientist_admin", ADMIN_PASSWORD)
        # The web (root here) seeds the job's data through the ownership
        # helper, as it does for the chat request files.
        script = (
            "import os\n"
            "from pathlib import Path\n"
            "from openscientist.job_container.identity import apply_job_ownership\n"
            "from openscientist.orchestrator.setup import create_job\n"
            "job = create_job(os.environ['JOB_ID'], 'Question?', [], 5, jobs_dir=Path('/jobs'))\n"
            "seed = job / 'data' / 'seed.csv'\n"
            "seed.write_text('topsecret-data\\n', encoding='utf-8')\n"
            "apply_job_ownership(seed)\n"
            "print('setup ok')\n"
        )
        # host networking reaches the test database on the host's loopback, and
        # root is the web's identity, the one that chowns the tree.
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["python", "-c", script],
            user="root",
            environment={
                "JOB_ID": str(uuid4()),
                "DATABASE_URL": test_database_url,
                "ADMIN_DATABASE_URL": admin_url,
                "OPENSCIENTIST_PROVIDER": "anthropic",
                "OPENSCIENTIST_SECRET_KEY": TEST_SECRET_KEY,
            },
            volumes={str(jobs_dir): {"bind": "/jobs", "mode": "rw"}},
            network_mode="host",
            remove=True,
        )
        assert "setup ok" in output.decode("utf-8"), output.decode("utf-8", errors="replace")

        try:
            yield jobs_dir
        finally:
            # The tree is agent-owned, so a host user outside the jobs group
            # cannot delete it.
            docker_client.containers.run(
                image=agent_image,
                entrypoint=["sh", "-c", "find /jobs -mindepth 1 -delete"],
                user="root",
                volumes={str(jobs_dir): {"bind": "/jobs", "mode": "rw"}},
                network_mode="none",
                remove=True,
            )

    @staticmethod
    def _job_dir(jobs_dir: Path) -> Path:
        dirs = [p for p in jobs_dir.iterdir() if p.is_dir()]
        assert len(dirs) == 1
        return dirs[0]

    def test_agent_user_can_write_the_job_tree(
        self, docker_client, agent_image, job_tree: Path
    ) -> None:
        job_dir = self._job_dir(job_tree)
        command = (
            "echo agent > /jobs/{name}/CLAUDE.md"
            " && echo a > /jobs/{name}/data/agent.csv"
            " && echo p > /jobs/{name}/provenance/agent.txt"
            " && cat /jobs/{name}/data/seed.csv"
            " && cat /jobs/{name}/data/agent.csv"
            " && git -C /jobs/{name} status --short > /dev/null"
            " && echo AGENT_OK"
        ).format(name=job_dir.name)
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", command],
            user="agent",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        payload = output.decode("utf-8")
        assert "AGENT_OK" in payload
        # The agent's own read-back of the seed and of the file it wrote.
        assert "topsecret-data\na\nAGENT_OK" in payload, payload

    def test_executor_user_reads_data_and_writes_provenance(
        self, docker_client, agent_image, job_tree: Path
    ) -> None:
        job_dir = self._job_dir(job_tree)
        command = (
            "cat /jobs/{name}/data/seed.csv"
            " && echo e > /jobs/{name}/provenance/executor.txt"
            " && echo EXECUTOR_OK"
        ).format(name=job_dir.name)
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", command],
            user=f"{EXECUTOR_UID}:{AGENT_GID}",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        payload = output.decode("utf-8")
        assert "EXECUTOR_OK" in payload
        assert "topsecret-data" in payload

    def test_another_local_user_is_denied(self, docker_client, agent_image, job_tree: Path) -> None:
        job_dir = self._job_dir(job_tree)
        container = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", "cat /jobs/{name}/data/seed.csv".format(name=job_dir.name)],
            user="424242:424242",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            detach=True,
        )
        exit_code = int(container.wait(timeout=30)["StatusCode"])
        logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        container.remove(force=True)
        assert exit_code != 0
        assert "topsecret-data" not in logs

    def test_the_web_still_writes_into_the_job_tree(
        self, docker_client, agent_image, job_tree: Path
    ) -> None:
        """The web runs as root and must keep being able to write into the
        agent-owned tree, for example the chat request files."""
        job_dir = self._job_dir(job_tree)
        command = (
            "echo '{{}}' > /jobs/{name}/.chat_request_1.json"
            " && stat -c '%g' /jobs/{name}/.chat_request_1.json"
            " && echo WEB_OK"
        ).format(name=job_dir.name)
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", command],
            user="root",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        payload = output.decode("utf-8")
        assert "WEB_OK" in payload, payload
        # The setgid job dir hands even the web's own files the jobs group.
        assert f"{AGENT_GID}\nWEB_OK" in payload, payload

    def test_agent_entrypoints_umask_leaves_group_writable_files(
        self, docker_client, agent_image, job_tree: Path
    ) -> None:
        """Both entrypoint chains set umask 0007 before dropping to the agent,
        so a file the agent writes carries no world bits and the jobs group."""
        job_dir = self._job_dir(job_tree)
        name = job_dir.name

        # Firewall chain: the real script installs nftables, sets the umask,
        # then setprivs to the agent and runs the given command.
        fw_command = ("f=/jobs/{name}/fw-probe.txt; echo x > $f; stat -c '%a %G' $f").format(
            name=name
        )
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["/agent-firewall-entrypoint.sh", "sh", "-c", fw_command],
            user="root",
            cap_add=["NET_ADMIN"],
            security_opt=["no-new-privileges:true"],
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        assert "660 jobs" in output.decode("utf-8"), output.decode("utf-8", errors="replace")

        # Plain chain: the real entrypoint module sets the umask at import,
        # exactly as production does before the harness starts.
        py_probe = (
            "import pathlib, runpy; runpy.run_path('/agent-entrypoint.py', "
            "run_name='not-main'); p = pathlib.Path('/jobs/{name}/py-probe.txt'); "
            "p.write_text('x'); print(format(p.stat().st_mode & 0o777, 'o'), "
            "p.stat().st_gid)"
        ).format(name=name)
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["python", "-c", py_probe],
            user="agent",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        assert "660 1001" in output.decode("utf-8"), output.decode("utf-8", errors="replace")

    def test_executor_entrypoint_umask_leaves_group_writable_provenance(
        self, docker_client, agent_image, executor_image, job_tree: Path
    ) -> None:
        """A real run of the executor entrypoint writes its outputs under umask
        0007, so provenance files the agent user must read are not world
        readable."""
        import base64
        import json

        job_dir = self._job_dir(job_tree)
        output_dir = job_dir / "provenance"
        # The same launch form the container manager uses: the request is
        # base64 on stdin, because the shell would mangle the JSON quotes.
        payload: dict[str, object] = {
            "code": "open('/output/umask-probe.txt', 'w').write('x')",
            "language": "python",
            "data_path": None,
            "output_dir": "/output",
            "timeout": 60,
            "description": "",
            "iteration": 1,
            "data_files": [],
        }
        input_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        container = docker_client.containers.run(
            image=executor_image,
            command=[
                "sh",
                "-c",
                f"echo {input_b64} | base64 -d | python -m openscientist_executor",
            ],
            entrypoint=[],
            volumes={str(output_dir): {"bind": "/output", "mode": "rw"}},
            network_mode="none",
            detach=True,
        )
        exit_code = int(container.wait(timeout=120)["StatusCode"])
        container.remove(force=True)
        assert exit_code == 0
        # The agent reads the probe back, so its content, mode, and group are
        # checked in the view of the user who must read it.
        check = (
            "cat /output/umask-probe.txt"
            " && stat -c '%a %g' /output/umask-probe.txt"
            " && echo PROBE_OK"
        )
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", check],
            user="agent",
            volumes={str(output_dir): {"bind": "/output", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        probe_payload = output.decode("utf-8", errors="replace")
        lines = probe_payload.splitlines()
        assert lines[0] == f"x{0o660:o} {AGENT_GID}", probe_payload
        assert "PROBE_OK" in probe_payload, probe_payload

    def test_web_group_member_file_is_group_writable_to_agent(
        self, docker_client, agent_image, job_tree: Path
    ) -> None:
        """The launcher gives the web its own uid inside the jobs group. A file
        it creates through the real helper relies on group inheritance, so the
        agent can write into it without any chown."""
        job_dir = self._job_dir(job_tree)
        name = job_dir.name
        probe = (
            "from pathlib import Path; from openscientist.job_container.identity "
            "import apply_job_ownership; p = Path('/jobs/{name}/.chat_request_probe.json'); "
            "p.write_text('{{}}'); apply_job_ownership(p); "
            "print(format(p.stat().st_mode & 0o777, 'o'), p.stat().st_gid)"
        ).format(name=name)
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["python", "-c", probe],
            user="1002:1001",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        # The web's own view of the file it wrote through the helper.
        assert output.decode("utf-8").strip() == f"{0o660:o} {AGENT_GID}", output

        command = (
            "echo more >> /jobs/{name}/.chat_request_probe.json"
            " && cat /jobs/{name}/.chat_request_probe.json"
            " && echo AGENT_OK"
        ).format(name=name)
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", command],
            user="agent",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        payload = output.decode("utf-8")
        assert "AGENT_OK" in payload, payload
        # The web's write carries no trailing newline, so the agent's append
        # lands on the same line.
        assert "{}more\nAGENT_OK" in payload, payload

    def test_run_home_is_agent_private(
        self,
        docker_client,
        agent_image,
        job_tree: Path,
        test_database_url: str,
    ) -> None:
        """A run home is owner-only, so agent access rests on real ownership,
        which only the root setup inside the container can establish."""
        script = (
            "from pathlib import Path\n"
            "from openscientist.job_container.run_home import create_run_home\n"
            "home = create_run_home(Path('/jobs'), 'agent_' + '1' * 32)\n"
            "st = home.stat()\n"
            "print('home', format(st.st_mode & 0o777, 'o'), st.st_uid, st.st_gid)\n"
            "print('run home ok')\n"
        )
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["python", "-c", script],
            user="root",
            environment={
                "DATABASE_URL": test_database_url,
                "OPENSCIENTIST_PROVIDER": "anthropic",
                "OPENSCIENTIST_SECRET_KEY": TEST_SECRET_KEY,
            },
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        # The ownership is read inside the root container, because the home
        # is 700 agent-owned and a non-root host user cannot stat into it.
        payload = output.decode("utf-8", errors="replace")
        assert "run home ok" in payload, payload
        assert f"home {0o700:o} {AGENT_UID} {AGENT_GID}" in payload, payload

        role = "agent_" + "1" * 32
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=[
                "sh",
                "-c",
                f"echo secret > /jobs/.run-homes/{role}/probe && "
                f"cat /jobs/.run-homes/{role}/probe && echo AGENT_OK",
            ],
            user="agent",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        payload = output.decode("utf-8")
        assert "AGENT_OK" in payload
        assert "secret" in payload

        container = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", f"cat /jobs/.run-homes/{role}/probe"],
            user="424242:424242",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            detach=True,
        )
        exit_code = int(container.wait(timeout=30)["StatusCode"])
        logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        container.remove(force=True)
        assert exit_code != 0
        assert "secret" not in logs


#: Userland for the non-root deploy tests, pinned the way
#: tests/test_launcher_jobs_mount.py pins its runtime image.
DEPLOY_TEST_IMAGE = (
    "python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30"
)


@pytest.fixture(scope="module")
def deploy_docker():
    """The Docker client for the non-root deploy tests, or a skip."""
    import docker

    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        pytest.skip("Docker not available")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def deploy_test_image(deploy_docker):
    import docker

    tag = f"openscientist-deploy-test-{uuid4().hex[:12]}"
    try:
        deploy_docker.images.build(
            path=str(REPO_ROOT),
            dockerfile="docker/Dockerfile.deploy-test",
            tag=tag,
            rm=True,
        )
    except docker.errors.BuildError as exc:
        log_tail = "\n".join(
            str(entry.get("stream", "")).rstrip()
            for entry in list(exc.build_log)[-20:]
            if isinstance(entry, dict)
        )
        pytest.fail(f"Failed to build the deploy test image {tag}:\n{log_tail}")
    try:
        yield tag
    finally:
        with suppress(Exception):
            deploy_docker.images.remove(tag, force=True)


def _deploy_volumes(jobs: Path) -> dict:
    """This checkout's interpreter and sources at their own paths, read-only,
    with the jobs tree read-write at /jobs."""
    base_prefix = str(Path(sys.base_prefix))
    return {
        str(REPO_ROOT): {"bind": str(REPO_ROOT), "mode": "ro"},
        base_prefix: {"bind": base_prefix, "mode": "ro"},
        str(jobs): {"bind": "/jobs", "mode": "rw"},
    }


def _run_deploy_snippet(
    deploy_docker,
    image: str,
    jobs: Path,
    *,
    user: str,
    snippet: str,
    environment: dict[str, str] | None = None,
    workdir: str = "",
) -> str:
    """A python snippet under uv's interpreter, as the given uid:gid. A job
    tree workdir keeps the snippet out of the checkout's dangling worktree
    gitdir, which git refuses to resolve."""
    output = deploy_docker.containers.run(
        image,
        [str(VENV_PYTHON), "-c", snippet],
        user=user,
        entrypoint=[],
        working_dir=workdir or str(REPO_ROOT),
        environment=environment or {},
        volumes=_deploy_volumes(jobs),
        network_mode="none",
        remove=True,
    )
    payload: str = output.decode("utf-8")
    return payload


def _group_jobs_root(jobs: Path, deploy_docker, image: str) -> Path:
    """The deployed ./jobs, a group-1001 setgid tree only root can chgrp."""
    deploy_docker.containers.run(
        image,
        [
            "sh",
            "-c",
            f"chown {os.getuid()}:{AGENT_GID} /jobs"
            f" && chmod {JOB_DIR_MODE:o} /jobs"
            f" && mkdir -p /jobs/.agent-home"
            f" && chown {AGENT_UID}:{AGENT_GID} /jobs/.agent-home"
            f" && chmod 700 /jobs/.agent-home",
        ],
        user="root",
        entrypoint=[],
        volumes={str(jobs): {"bind": "/jobs", "mode": "rw"}},
        network_mode="none",
        remove=True,
    )
    return jobs


@pytest.mark.integration
class TestNonRootWebRunHome:
    """The deployed web is uid 1002 in the jobs group and cannot chown, so its
    run homes take the agent's access from the group."""

    @pytest.fixture()
    def jobs(
        self, deploy_test_image, deploy_docker, tmp_path_factory
    ) -> Generator[Path, None, None]:
        jobs = tmp_path_factory.mktemp("deploy-jobs")
        try:
            yield _group_jobs_root(jobs, deploy_docker, deploy_test_image)
        finally:
            # The homes are the web's, so a host user outside the jobs group
            # cannot delete them.
            deploy_docker.containers.run(
                deploy_test_image,
                ["sh", "-c", "find /jobs -mindepth 1 -delete"],
                user="root",
                entrypoint=[],
                volumes={str(jobs): {"bind": "/jobs", "mode": "rw"}},
                network_mode="none",
                remove=True,
            )

    def test_agent_writes_a_web_created_run_home(
        self, deploy_test_image, deploy_docker, jobs: Path
    ) -> None:
        """The E2E failure in one place. The web creates the homes as 1002, the
        agent writes into them as 1001 of the jobs group, and outsiders read
        nothing."""
        role_omp = "agent_" + "1" * 32
        role_codex = "agent_" + "2" * 32
        snippet = (
            "import os\n"
            "from pathlib import Path\n"
            "from openscientist.job_container.run_home import create_run_home\n"
            "roles = [os.environ['ROLE_OMP'], os.environ['ROLE_CODEX']]\n"
            "for role in roles:\n"
            "    home = create_run_home(Path('/jobs'), role)\n"
            "    st = home.stat()\n"
            "    print('home', role, format(st.st_mode & 0o777, 'o'), st.st_uid, st.st_gid)\n"
        )
        output = _run_deploy_snippet(
            deploy_docker,
            deploy_test_image,
            jobs,
            user="1002:1001",
            snippet=snippet,
            environment={"ROLE_OMP": role_omp, "ROLE_CODEX": role_codex},
        )
        # The non-root web keeps ownership, so the homes hand the agent their
        # access through the jobs group at 0770, owner-only 0700 being the
        # root-web mode.
        assert f"home {role_omp} 770 1002 1001" in output, output
        assert f"home {role_codex} 770 1002 1001" in output, output

        command = (
            f"echo omp-secret > /jobs/.run-homes/{role_omp}/models.yml"
            f" && mkdir -p /jobs/.run-homes/{role_codex}/.config"
            f" && echo codex-secret > /jobs/.run-homes/{role_codex}/.config/auth.json"
            f" && cat /jobs/.run-homes/{role_omp}/models.yml"
            f" && stat -c '%g' /jobs/.run-homes/{role_omp}/models.yml"
            f" && echo AGENT_OK"
        )
        output = deploy_docker.containers.run(
            deploy_test_image,
            ["sh", "-c", command],
            user=f"{AGENT_UID}:{AGENT_GID}",
            entrypoint=[],
            volumes=_deploy_volumes(jobs),
            network_mode="none",
            remove=True,
        ).decode("utf-8")
        assert "AGENT_OK" in output, output
        # The agent's read-back and group of the file it wrote.
        assert f"omp-secret\n{AGENT_GID}\nAGENT_OK" in output, output

        container = deploy_docker.containers.run(
            deploy_test_image,
            [
                "sh",
                "-c",
                f"cat /jobs/.run-homes/{role_omp}/models.yml"
                f" && cat /jobs/.run-homes/{role_codex}/.config/auth.json",
            ],
            user="424242:424242",
            entrypoint=[],
            volumes=_deploy_volumes(jobs),
            network_mode="none",
            detach=True,
        )
        exit_code = int(container.wait(timeout=30)["StatusCode"])
        logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        container.remove(force=True)
        assert exit_code != 0
        assert "omp-secret" not in logs
        assert "codex-secret" not in logs


@pytest.mark.integration
class TestNonRootWebGit:
    """The web inits the job's git repository as uid 1002, so the agent's git
    meets the ownership guard without the entrypoint's trust."""

    @pytest.fixture()
    def job_dir(self, deploy_test_image, deploy_docker, tmp_path) -> Generator[Path, None, None]:
        jobs: Path = tmp_path / "jobs"
        jobs.mkdir()
        _group_jobs_root(jobs, deploy_docker, deploy_test_image)
        job_id = str(uuid4())
        # The git block of create_job's setup, as the non-root web.
        snippet = (
            "import os\n"
            "import subprocess\n"
            "from pathlib import Path\n"
            "from openscientist.job_container.identity import ("
            "apply_job_ownership, apply_job_ownership_tree)\n"
            "job = Path('/jobs/' + os.environ['JOB_ID'])\n"
            "job.mkdir()\n"
            "apply_job_ownership(job)\n"
            "subprocess.run(['git', 'init'], cwd=str(job), capture_output=True, check=True)\n"
            "apply_job_ownership_tree(job / '.git')\n"
            "st = (job / '.git').stat()\n"
            "print('web setup ok')\n"
            "print('git-owner', st.st_uid, st.st_gid)\n"
        )
        output = _run_deploy_snippet(
            deploy_docker,
            deploy_test_image,
            jobs,
            user="1002:1001",
            snippet=snippet,
            environment={"JOB_ID": job_id},
        )
        assert "web setup ok" in output, output
        # The non-root web keeps the ownership git's guard then trips on.
        assert f"git-owner {1002} {AGENT_GID}" in output, output
        job = jobs / job_id
        try:
            yield job
        finally:
            # The job dir is the web's, so a host user outside the jobs group
            # cannot delete it.
            deploy_docker.containers.run(
                deploy_test_image,
                ["sh", "-c", "find /jobs -mindepth 1 -delete"],
                user="root",
                entrypoint=[],
                volumes={str(jobs): {"bind": "/jobs", "mode": "rw"}},
                network_mode="none",
                remove=True,
            )

    def test_agent_git_runs_in_the_web_repo(
        self, deploy_test_image, deploy_docker, job_dir: Path
    ) -> None:
        jobs = job_dir.parent
        snippet = (
            "import os\n"
            "import runpy\n"
            "import subprocess\n"
            "from pathlib import Path\n"
            f"m = runpy.run_path({str(REPO_ROOT / 'docker' / 'agent-entrypoint.py')!r}, run_name='not-main')\n"
            "trust = m.get('trust_job_repository')\n"
            "job = '/jobs/' + os.environ['JOB_ID']\n"
            "if trust is not None:\n"
            "    trust(Path(job))\n"
            "r = subprocess.run(['git', '-C', job, 'status'], capture_output=True, text=True)\n"
            "print('git exit', r.returncode)\n"
            "print(r.stderr)\n"
        )
        output = _run_deploy_snippet(
            deploy_docker,
            deploy_test_image,
            jobs,
            user=f"{AGENT_UID}:{AGENT_GID}",
            snippet=snippet,
            environment={"JOB_ID": job_dir.name, "HOME": "/jobs/.agent-home"},
            workdir="/jobs",
        )
        assert "git exit 0" in output, output
        assert "dubious ownership" not in output, output


class TestAgentEntrypointGitTrust:
    """The entrypoint's main wires the git trust in, where production reaches it."""

    def test_main_trusts_the_job_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import runpy

        from openscientist.orchestrator import discovery

        job_dir = tmp_path / "job"
        job_dir.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        seen: dict[str, Path] = {}

        async def fake_discovery(job: Path) -> dict:
            seen["job"] = job
            return {"status": "completed", "iterations": 0, "findings": 0}

        monkeypatch.setattr(discovery, "run_discovery_async", fake_discovery)
        monkeypatch.setenv("JOB_ID", "1" * 32)
        monkeypatch.setenv("JOB_DIR", str(job_dir))
        monkeypatch.delenv("OPENSCIENTIST_RUN_MODE", raising=False)
        monkeypatch.setenv("HOME", str(home))

        old_umask = os.umask(0o022)
        try:
            entrypoint = runpy.run_path(
                str(REPO_ROOT / "docker" / "agent-entrypoint.py"), run_name="not-main"
            )
            assert asyncio.run(entrypoint["main"]()) == 0
        finally:
            os.umask(old_umask)

        assert seen["job"] == job_dir
        assert str(job_dir) in (home / ".gitconfig").read_text(encoding="utf-8")

"""Ownership and modes of the job tree the web creates for the job containers.

The web process runs as root and hands every file it creates in the job tree
to the containers' fixed users, stripping the world bits. Non-root tests
observe the chown on a real temp dir; root tests verify the real ownership.
"""

from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.job_container.identity import (
    AGENT_GID,
    AGENT_UID,
    EXECUTOR_UID,
    JOB_DIR_MODE,
    JOB_FILE_MODE,
    PRIVATE_FILE_MODE,
    RUN_HOME_DIR_MODE,
    apply_job_ownership,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
IS_ROOT = os.geteuid() == 0


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
    """Run homes hold operator logins, so they are owner-only for the agent."""

    def test_run_home_is_agent_owned_and_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.job_container.run_home import create_run_home

        chowns = ChownRecorder(monkeypatch)
        home = create_run_home(tmp_path / "jobs", "agent_" + "1" * 32)

        for path in (home, home.parent):
            assert _mode(path) == RUN_HOME_DIR_MODE, path
            assert chowns.owned(path), path


class TestCopiedLogins:
    """Operator logins copied into a run home are the agent's private files."""

    def test_codex_login_is_agent_owned_and_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.agent.codex_agent import CodexAgent
        from openscientist.job_container.run_home import create_run_home
        from openscientist.settings import get_settings

        chowns = ChownRecorder(monkeypatch)
        src = tmp_path / "host-auth.json"
        src.write_text('{"tokens": {}}', encoding="utf-8")
        home = create_run_home(tmp_path / "jobs", "agent_" + "1" * 32)
        monkeypatch.setenv("CODEX_AUTH_HOST_PATH", str(src))
        get_settings.cache_clear()
        try:
            CodexAgent.provision_run_home(get_settings(), home)
        finally:
            get_settings.cache_clear()

        dest = home / "auth.json"
        assert dest.read_text(encoding="utf-8") == '{"tokens": {}}'
        assert _mode(dest) == PRIVATE_FILE_MODE
        assert chowns.owned(dest)

    def test_omp_vault_files_are_agent_owned_and_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.agent.omp_agent import OmpAgent
        from openscientist.job_container.run_home import create_run_home
        from openscientist.settings import get_settings

        chowns = ChownRecorder(monkeypatch)
        src = tmp_path / "omp-store"
        src.mkdir()
        for name in ("agent.db", "agent.db-wal", "config.yml"):
            (src / name).write_text(name, encoding="utf-8")
        home = create_run_home(tmp_path / "jobs", "agent_" + "1" * 32)
        monkeypatch.setenv("OMP_AUTH_HOST_PATH", str(src))
        get_settings.cache_clear()
        try:
            OmpAgent.provision_run_home(get_settings(), home)
        finally:
            get_settings.cache_clear()

        for name in ("agent.db", "agent.db-wal", "config.yml"):
            dest = home / name
            assert _mode(dest) == PRIVATE_FILE_MODE, name
            assert chowns.owned(dest), name


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
            log_tail = "\n".join(
                str(entry.get("stream", "")).rstrip()
                for entry in exc.build_log[-20:]
                if isinstance(entry, dict)
            )
            pytest.fail(f"Failed to build test agent image {tag}:\n{log_tail}")

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
        script = (
            "import os\n"
            "from pathlib import Path\n"
            "from openscientist.orchestrator.setup import create_job\n"
            "create_job(os.environ['JOB_ID'], 'Question?', [], 5, jobs_dir=Path('/jobs'))\n"
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
                "OPENSCIENTIST_SECRET_KEY": "job-perms-test-secret",
            },
            volumes={str(jobs_dir): {"bind": "/jobs", "mode": "rw"}},
            network_mode="host",
            remove=True,
        )
        assert "setup ok" in output.decode("utf-8"), output.decode("utf-8", errors="replace")

        yield jobs_dir

    @staticmethod
    def _job_dir(jobs_dir: Path) -> Path:
        dirs = [p for p in jobs_dir.iterdir() if p.is_dir()]
        assert len(dirs) == 1
        return dirs[0]

    def test_agent_user_can_write_the_job_tree(
        self, docker_client, agent_image, job_tree: Path
    ) -> None:
        job_dir = self._job_dir(job_tree)
        (job_dir / "data" / "seed.csv").write_text("topsecret-data\n", encoding="utf-8")
        command = (
            "echo agent > /jobs/{name}/CLAUDE.md"
            " && echo a > /jobs/{name}/data/agent.csv"
            " && echo p > /jobs/{name}/provenance/agent.txt"
            " && cat /jobs/{name}/data/seed.csv"
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
        assert "topsecret-data" in payload
        assert (job_dir / "data" / "agent.csv").read_text(encoding="utf-8").strip() == "a"

    def test_executor_user_reads_data_and_writes_provenance(
        self, docker_client, agent_image, job_tree: Path
    ) -> None:
        job_dir = self._job_dir(job_tree)
        (job_dir / "data" / "seed.csv").write_text("topsecret-data\n", encoding="utf-8")
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
        (job_dir / "data" / "seed.csv").write_text("topsecret-data\n", encoding="utf-8")
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
        command = "echo '{{}}' > /jobs/{name}/.chat_request_1.json && echo WEB_OK".format(
            name=job_dir.name
        )
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", command],
            user="root",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        assert "WEB_OK" in output.decode("utf-8")
        request = job_dir / ".chat_request_1.json"
        assert request.exists()
        # The setgid job dir hands even the web's own files the jobs group.
        assert request.stat().st_gid == AGENT_GID

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
        self, docker_client, job_tree: Path
    ) -> None:
        """A real run of the executor entrypoint writes its outputs under umask
        0007, so provenance files the agent user must read are not world
        readable."""
        import base64
        import json

        docker_client.images.get("openscientist-executor:latest")
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
            image="openscientist-executor:latest",
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
        probe = output_dir / "umask-probe.txt"
        assert probe.read_text(encoding="utf-8") == "x"
        st = probe.stat()
        assert (st.st_mode & 0o777) == 0o660
        assert st.st_gid == AGENT_GID

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
            "print(format(p.stat().st_mode & 0o777, 'o'))"
        ).format(name=name)
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["python", "-c", probe],
            user="1002:1001",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        assert "660" in output.decode("utf-8"), output.decode("utf-8", errors="replace")
        request = job_dir / ".chat_request_probe.json"
        assert (request.stat().st_mode & 0o777) == 0o660
        assert request.stat().st_gid == AGENT_GID

        command = "echo more >> /jobs/{name}/.chat_request_probe.json && echo AGENT_OK".format(
            name=name
        )
        output = docker_client.containers.run(
            image=agent_image,
            entrypoint=["sh", "-c", command],
            user="agent",
            volumes={str(job_tree): {"bind": "/jobs", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )
        assert "AGENT_OK" in output.decode("utf-8")
        # The web's write carries no trailing newline, so the agent's append
        # lands on the same line.
        assert request.read_text(encoding="utf-8") == "{}more\n"

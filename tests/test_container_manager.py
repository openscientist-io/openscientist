"""
Tests for container_manager module.

The unit tests mock the launcher client; the integration tests run the real
launcher process and the real executor image, the way the launcher tests do.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

import docker
from openscientist import container_manager
from openscientist.job_container.launcher_client import (
    LauncherBadRequestError,
    LauncherImageMissingError,
    LauncherUnavailableError,
)
from openscientist.settings import clear_settings_cache
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.helpers import assert_names_a_build_command
from tests.launcher import (
    EXECUTOR_IMAGE,
    LAUNCHER_INSTANCE_ID,
    Launcher,
    executor_image_problem,
    image_digest,
)

JOB_ID = "11111111-2222-3333-4444-555555555555"


@pytest_asyncio.fixture(autouse=True)
async def admin_settings_row(test_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """The row each execution reads its limits and the airgap flag from."""
    await set_admin_settings_row(test_engine)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


def _result_json(**overrides: Any) -> str:
    result: dict[str, Any] = {
        "success": True,
        "output": "Hello, World!",
        "plots": [],
        "error": None,
        "execution_time": 0.5,
    }
    result.update(overrides)
    return json.dumps(result)


def _launcher_entry(name: str, created: str, **labels: str) -> dict[str, Any]:
    return {
        "id": name.replace("openscientist-", "")[:12],
        "name": name,
        "status": "exited",
        "created": created,
        "labels": {
            "openscientist.job_id": JOB_ID,
            "openscientist.type": "executor",
            "openscientist.launcher": LAUNCHER_INSTANCE_ID,
            **labels,
        },
    }


class TestContainerManagerUnit:
    """Unit tests with a mocked launcher client."""

    @pytest.fixture
    def jobs_root(self, tmp_path: Path) -> Path:
        jobs = tmp_path / "jobs"
        (jobs / JOB_ID).mkdir(parents=True)
        return jobs

    @pytest.fixture
    def manager(self, jobs_root: Path) -> tuple[Any, MagicMock]:
        """A manager whose client is a mock, with a job dir for its runs."""
        from openscientist.container_manager import ContainerManager

        mgr = ContainerManager()
        client = MagicMock()
        mgr._client = client
        return mgr, client

    def test_init_defaults(self):
        """The limits are not copied at init; the row supplies them at execution."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()
        assert manager.image == "openscientist-executor:latest"
        assert manager.memory_mib is None
        assert manager.cpu_limit is None
        assert manager.timeout is None

    def test_init_custom_values(self):
        """Test ContainerManager accepts custom values."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager(
            image="custom:latest",
            memory_mib=4096,
            cpu_limit=1.0,
            timeout=300,
        )
        assert manager.image == "custom:latest"
        assert manager.memory_mib == 4096
        assert manager.cpu_limit == 1.0
        assert manager.timeout == 300

    def test_execute_code_success(self, manager: tuple[Any, MagicMock], jobs_root: Path) -> None:
        """The result JSON the launcher captured is parsed and returned."""
        mgr, client = manager
        client.run_exec.return_value = {"stdout": _result_json(), "exit_code": 0}

        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = mgr.execute_code(code="print('hi')", job_id=JOB_ID, output_dir=out_dir)

        assert result["success"] is True
        assert result["output"] == "Hello, World!"
        assert result["error"] is None
        assert client.run_exec.call_args.args[0]["pids_limit"] == 4096

    def test_execute_code_failure(self, manager: tuple[Any, MagicMock], jobs_root: Path) -> None:
        """A failed run passes the executor's error through."""
        mgr, client = manager
        client.run_exec.return_value = {
            "stdout": _result_json(
                success=False,
                output="",
                error="NameError: name 'undefined' is not defined",
            ),
            "exit_code": 0,
        }

        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = mgr.execute_code(code="undefined", job_id=JOB_ID, output_dir=out_dir)

        assert result["success"] is False
        assert "NameError" in result["error"]

    def test_execute_code_image_not_found(
        self, manager: tuple[Any, MagicMock], jobs_root: Path
    ) -> None:
        """A missing template image names the image and the build command."""
        mgr, client = manager
        client.run_exec.side_effect = LauncherImageMissingError(
            "image_missing", f"image {mgr.image} is not available"
        )

        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = mgr.execute_code(code="print('test')", job_id=JOB_ID, output_dir=out_dir)

        assert result["success"] is False
        assert mgr.image in result["error"]
        assert_names_a_build_command(result["error"])

    def test_execute_code_launcher_unavailable(
        self, manager: tuple[Any, MagicMock], jobs_root: Path
    ) -> None:
        """A launcher that cannot be reached surfaces a clean error result."""
        mgr, client = manager
        client.run_exec.side_effect = LauncherUnavailableError(
            "unavailable", "the launcher could not be reached"
        )

        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = mgr.execute_code(code="print('test')", job_id=JOB_ID, output_dir=out_dir)

        assert result["success"] is False
        assert "launcher" in result["error"].lower()

    def test_execute_code_refused_request(
        self, manager: tuple[Any, MagicMock], jobs_root: Path
    ) -> None:
        """A request the launcher refuses surfaces its refusal."""
        mgr, client = manager
        client.run_exec.side_effect = LauncherBadRequestError(
            "bad_request", "output_dir is outside the job directory"
        )

        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = mgr.execute_code(code="print('test')", job_id=JOB_ID, output_dir=out_dir)

        assert result["success"] is False
        assert "outside the job directory" in result["error"]

    def test_cleanup_job_containers(self, manager: tuple[Any, MagicMock]) -> None:
        """Each launcher-created container of the job is removed once."""
        mgr, client = manager
        old = _launcher_entry("openscientist-exec-job123-abc", "")
        new = _launcher_entry("openscientist-exec-job123-def", "")
        client.list_containers.return_value = [old, new]

        removed = mgr.cleanup_job_containers(JOB_ID)

        assert removed == 2
        client.remove.assert_any_call(old["id"])
        client.remove.assert_any_call(new["id"])

    def test_cleanup_job_containers_survives_a_refused_remove(
        self, manager: tuple[Any, MagicMock]
    ) -> None:
        mgr, client = manager
        old = _launcher_entry("openscientist-exec-job123-abc", "")
        other = _launcher_entry("openscientist-exec-job123-def", "")
        client.list_containers.return_value = [old, other]
        client.remove.side_effect = [
            LauncherUnavailableError("unavailable", "down"),
            None,
        ]

        assert mgr.cleanup_job_containers(JOB_ID) == 1
        client.remove.assert_any_call(other["id"])

    def test_cleanup_orphaned_containers(self, manager: tuple[Any, MagicMock]) -> None:
        """Only executor containers older than the bound are removed."""
        mgr, client = manager
        old_time = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        recent_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        old = _launcher_entry("openscientist-exec-old", old_time)
        recent = _launcher_entry("openscientist-exec-recent", recent_time)
        client.list_containers.return_value = [old, recent]

        removed = mgr.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1
        client.remove.assert_called_once_with(old["id"])

    def test_check_image_available_true(self, manager: tuple[Any, MagicMock]) -> None:
        mgr, client = manager
        client.health.return_value = {"docker": True, "images": {"agent": True, "executor": True}}
        assert mgr.check_image_available() is True

    def test_check_image_available_false(self, manager: tuple[Any, MagicMock]) -> None:
        mgr, client = manager
        client.health.return_value = {"docker": True, "images": {"agent": True, "executor": False}}
        assert mgr.check_image_available() is False

    def test_check_image_available_launcher_down(self, manager: tuple[Any, MagicMock]) -> None:
        mgr, client = manager
        client.health.side_effect = LauncherUnavailableError("unavailable", "down")
        assert mgr.check_image_available() is False

    def test_is_available_true(self, manager: tuple[Any, MagicMock]) -> None:
        mgr, client = manager
        client.health.return_value = {"docker": True, "images": {"agent": False, "executor": False}}
        assert mgr.is_available() is True

    def test_is_available_false(self, manager: tuple[Any, MagicMock]) -> None:
        mgr, client = manager
        client.health.side_effect = LauncherUnavailableError("unavailable", "down")
        assert mgr.is_available() is False


class TestToHostPath:
    """Unit tests for the shared to_host_path function."""

    def test_path_under_container_app_dir_is_translated(self) -> None:
        from openscientist.job_container.utils import to_host_path

        cs = MagicMock()
        cs.host_project_dir = "/host/project"
        cs.container_app_dir = "/app"

        result = to_host_path(Path("/app/jobs/123/data"), cs)
        assert result == Path("/host/project/jobs/123/data")

    def test_path_not_under_container_app_dir_unchanged(self) -> None:
        from openscientist.job_container.utils import to_host_path

        cs = MagicMock()
        cs.host_project_dir = "/host/project"
        cs.container_app_dir = "/app"

        result = to_host_path(Path("/tmp/something"), cs)
        assert result == Path("/tmp/something")

    def test_no_host_project_dir_returns_unchanged(self) -> None:
        from openscientist.job_container.utils import to_host_path

        cs = MagicMock()
        cs.host_project_dir = None
        cs.container_app_dir = "/app"

        result = to_host_path(Path("/app/jobs/123/data"), cs)
        assert result == Path("/app/jobs/123/data")


class TestExecuteCodeRequest:
    """The launcher request carries the row's values and host paths."""

    @pytest.fixture
    def manager(self, tmp_path: Path) -> tuple[Any, MagicMock]:
        from openscientist.container_manager import ContainerManager

        mgr = ContainerManager()
        client = MagicMock()
        mgr._client = client
        client.run_exec.return_value = {"stdout": _result_json(), "exit_code": 0}
        return mgr, client

    @pytest.fixture
    def jobs_root(self, tmp_path: Path) -> Path:
        jobs = tmp_path / "jobs"
        (jobs / JOB_ID).mkdir(parents=True)
        return jobs

    def _exec(self, mgr: Any, tmp_path: Path, **overrides: Any) -> None:
        out_dir = tmp_path / "jobs" / JOB_ID / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "code": "print('hi')",
            "job_id": JOB_ID,
            "output_dir": str(out_dir),
        }
        kwargs.update(overrides)
        mgr.execute_code(**kwargs)

    @pytest.mark.asyncio
    async def test_row_limits_reach_the_launcher(
        self, manager: tuple[Any, MagicMock], tmp_path: Path, test_engine: AsyncEngine
    ) -> None:
        """The admin row's limits, timeout, and airgap reach the launcher request."""
        mgr, client = manager
        await set_admin_settings_row(test_engine)
        # The execution reads the row on a worker thread, like the broker does.
        await asyncio.to_thread(self._exec, mgr, tmp_path)
        request = client.run_exec.call_args.args[0]
        assert request["memory_mib"] == 512
        assert request["cpu"] == 0.5
        assert request["airgapped"] is False
        assert set(request["egress_allow"]) == {"openscientist:8081", "openscientist:8082"}
        # The executor's own alarm is the timeout; the launcher's bound clears it.
        assert request["timeout_seconds"] == 150
        assert request["input"]["timeout"] == 120

        await set_admin_settings_row(
            test_engine,
            executor_cpu=0.75,
            executor_memory_mib=1024,
            executor_timeout_seconds=60,
            airgapped=True,
        )
        await asyncio.to_thread(self._exec, mgr, tmp_path)
        request = client.run_exec.call_args.args[0]
        assert request["memory_mib"] == 1024
        assert request["cpu"] == 0.75
        assert request["airgapped"] is True
        # With no network there is nothing for the launcher to allow.
        assert "egress_allow" not in request
        assert request["timeout_seconds"] == 90
        assert request["input"]["timeout"] == 60

    def test_host_paths_reach_the_launcher(
        self, manager: tuple[Any, MagicMock], tmp_path: Path
    ) -> None:
        """Container-app paths are translated to host paths before the request."""
        from unittest.mock import patch

        mgr, client = manager
        app_dir = tmp_path / "app"
        host_dir = tmp_path / "host_project"
        app_dir.mkdir()
        data_dir = app_dir / "data"
        data_dir.mkdir()
        data_file = data_dir / "file.csv"
        data_file.write_text("a,b\n1,2\n", encoding="utf-8")
        fake_settings = MagicMock()
        fake_settings.container.container_app_dir = str(app_dir)
        fake_settings.container.host_project_dir = str(host_dir)

        with patch("openscientist.container_manager.get_settings", return_value=fake_settings):
            self._exec(mgr, app_dir, data_files=[{"path": str(data_file), "name": "file.csv"}])

        request = client.run_exec.call_args.args[0]
        assert request["input"]["output_dir"] == str(host_dir / "jobs" / JOB_ID / "out")
        assert request["input"]["data_files"][0]["path"] == str(host_dir / "data" / "file.csv")
        # The default row is not air-gapped, so the request carries the web's
        # firewall allow-list for the launcher's restrict firewall.
        assert set(request["egress_allow"]) == {"openscientist:8081", "openscientist:8082"}

    def test_missing_data_file_is_a_refused_request(
        self, manager: tuple[Any, MagicMock], tmp_path: Path
    ) -> None:
        mgr, client = manager
        client.run_exec.side_effect = LauncherBadRequestError(
            "bad_request", "a data file is missing"
        )
        out_dir = tmp_path / "jobs" / JOB_ID / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = mgr.execute_code(
            code="print('hi')",
            job_id=JOB_ID,
            output_dir=str(out_dir),
            data_files=[{"path": str(tmp_path / "ghost.csv"), "name": "ghost.csv"}],
        )
        assert result["success"] is False
        assert "data file is missing" in result["error"]

    @pytest.mark.asyncio
    async def test_restricted_executor_request_carries_the_web_allowlist(
        self, manager: tuple[Any, MagicMock], tmp_path: Path, test_engine: AsyncEngine
    ) -> None:
        """Without air-gap the request carries the web's endpoints for the
        launcher's restrict firewall; the executor never opens Postgres."""
        mgr, client = manager
        await set_admin_settings_row(test_engine, airgapped=False)
        await asyncio.to_thread(self._exec, mgr, tmp_path)
        request = client.run_exec.call_args.args[0]
        assert request["airgapped"] is False
        assert set(request["egress_allow"]) == {"openscientist:8081", "openscientist:8082"}


class TestGetContainerManager:
    """Tests for the global container manager singleton."""

    def test_get_container_manager_returns_same_instance(self):
        """Test that get_container_manager returns a singleton."""
        from openscientist import container_manager

        # Reset global instance
        container_manager._container_manager = None

        manager1 = container_manager.get_container_manager()
        manager2 = container_manager.get_container_manager()

        assert manager1 is manager2

        # Clean up
        container_manager._container_manager = None


@pytest.mark.integration
class TestContainerManagerLauncherIntegration:
    """Integration tests against the real launcher process and Docker daemon.

    Run with: pytest tests/test_container_manager.py -v -m integration
    """

    @pytest.fixture(autouse=True)
    def check_docker(self):
        if problem := executor_image_problem():
            pytest.skip(problem)

    @pytest.fixture
    def jobs_root(self, tmp_path: Path) -> Path:
        jobs = tmp_path / "jobs"
        (jobs / JOB_ID).mkdir(parents=True)
        return jobs

    @pytest.fixture
    def launcher(self, tmp_path: Path, jobs_root: Path) -> Iterator[Launcher]:
        client = docker.from_env()
        digest = image_digest(client, EXECUTOR_IMAGE)
        started = Launcher.start(
            tmp_path,
            jobs_root,
            AGENT_IMAGE_DIGESTS=digest,
            EXECUTOR_IMAGE_DIGESTS=digest,
        )
        try:
            yield started
        finally:
            started.close()

    @pytest.fixture
    def manager(self, launcher: Launcher, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
        monkeypatch.setenv("OPENSCIENTIST_LAUNCHER_SOCKET_PATH", str(launcher.socket_path))
        clear_settings_cache()
        previous = container_manager._container_manager
        container_manager._container_manager = None
        try:
            yield container_manager.ContainerManager()
        finally:
            container_manager._container_manager = previous
            clear_settings_cache()

    def test_executor_run_end_to_end(self, manager: Any, jobs_root: Path) -> None:
        """One executor run through the real launcher, container removed after."""
        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = manager.execute_code(
            code="import time\ntime.sleep(2)\nprint('hello from the launcher path')",
            job_id=JOB_ID,
            output_dir=out_dir,
        )

        assert result["success"] is True
        assert "hello from the launcher path" in result["output"]
        # The launcher removed its container on completion.
        assert manager.cleanup_job_containers(JOB_ID) == 0

    def test_executor_run_reads_the_mounted_data_file(self, manager: Any, jobs_root: Path) -> None:
        job_dir = jobs_root / JOB_ID
        (job_dir / "study.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        out_dir = job_dir / "out"
        out_dir.mkdir()
        result = manager.execute_code(
            code="print(int(data['a'].sum()) + int(data['b'].sum()))",
            job_id=JOB_ID,
            output_dir=out_dir,
            data_path=str(job_dir / "study.csv"),
            data_files=[{"path": str(job_dir / "study.csv"), "name": "study.csv"}],
        )

        assert result["success"] is True
        assert "3" in result["output"]

    def test_missing_executor_image_surfaces_the_build_command(
        self, tmp_path: Path, jobs_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = Launcher.start(
            tmp_path,
            jobs_root,
            AGENT_IMAGE_DIGESTS=image_digest(docker.from_env(), EXECUTOR_IMAGE),
            EXECUTOR_IMAGE_DIGESTS="sha256:" + "0" * 64,
        )
        try:
            monkeypatch.setenv("OPENSCIENTIST_LAUNCHER_SOCKET_PATH", str(launcher.socket_path))
            clear_settings_cache()
            manager = container_manager.ContainerManager()
            out_dir = jobs_root / JOB_ID / "out"
            out_dir.mkdir()
            result = manager.execute_code(code="print(1)", job_id=JOB_ID, output_dir=out_dir)
        finally:
            launcher.close()
            clear_settings_cache()

        assert result["success"] is False
        assert_names_a_build_command(result["error"])

    def test_launcher_down_surfaces_a_clean_error(
        self, jobs_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "OPENSCIENTIST_LAUNCHER_SOCKET_PATH", str(jobs_root.parent / "absent" / "launcher.sock")
        )
        clear_settings_cache()
        try:
            manager = container_manager.ContainerManager()
            assert manager.is_available() is False
            assert manager.check_image_available() is False
            out_dir = jobs_root / JOB_ID / "out"
            out_dir.mkdir()
            result = manager.execute_code(code="print(1)", job_id=JOB_ID, output_dir=out_dir)
        finally:
            clear_settings_cache()

        assert result["success"] is False
        assert "launcher" in result["error"].lower()

    def test_cleanup_job_containers_removes_the_launcher_containers(
        self, manager: Any, jobs_root: Path
    ) -> None:
        client = docker.from_env()
        labelled = client.containers.create(
            EXECUTOR_IMAGE,
            labels={
                "openscientist.job_id": JOB_ID,
                "openscientist.type": "executor",
                "openscientist.launcher": LAUNCHER_INSTANCE_ID,
            },
        )
        foreign = client.containers.create(
            EXECUTOR_IMAGE,
            labels={
                "openscientist.job_id": JOB_ID,
                "openscientist.type": "executor",
            },
        )
        try:
            assert manager.cleanup_job_containers(JOB_ID) == 1
            # The foreign container is not the launcher's to remove.
            assert manager.cleanup_job_containers(JOB_ID) == 0
        finally:
            # The cleanup removed the labelled container already.
            try:
                labelled.remove(force=True)
            except docker.errors.NotFound:
                pass
            foreign.remove(force=True)

    def test_cleanup_orphaned_containers_reaps_the_old_one(
        self, manager: Any, jobs_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = docker.from_env()
        fresh = client.containers.create(
            EXECUTOR_IMAGE,
            labels={
                "openscientist.job_id": JOB_ID,
                "openscientist.type": "executor",
                "openscientist.launcher": LAUNCHER_INSTANCE_ID,
            },
        )

        class _AgedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=48)

        try:
            monkeypatch.setattr(container_manager, "datetime", _AgedNow)
            assert manager.cleanup_orphaned_containers(max_age_hours=24) == 1
        finally:
            # The reaper may already have removed the container.
            try:
                fresh.remove(force=True)
            except docker.errors.NotFound:
                pass

    def test_fresh_container_is_not_reaped(self, manager: Any, jobs_root: Path) -> None:
        client = docker.from_env()
        fresh = client.containers.create(
            EXECUTOR_IMAGE,
            labels={
                "openscientist.job_id": JOB_ID,
                "openscientist.type": "executor",
                "openscientist.launcher": LAUNCHER_INSTANCE_ID,
            },
        )
        try:
            assert manager.cleanup_orphaned_containers(max_age_hours=24) == 0
        finally:
            fresh.remove(force=True)

    def test_execute_with_numpy(self, manager: Any, jobs_root: Path) -> None:
        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = manager.execute_code(
            code="import numpy as np; print(np.array([1,2,3]).sum())",
            job_id=JOB_ID,
            output_dir=out_dir,
        )

        assert result["success"] is True
        assert "6" in result["output"]

    def test_execute_with_pandas(self, manager: Any, jobs_root: Path) -> None:
        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = manager.execute_code(
            code='import pandas as pd; df = pd.DataFrame({"a": [1,2,3]}); print(df.shape)',
            job_id=JOB_ID,
            output_dir=out_dir,
        )

        assert result["success"] is True
        assert "(3, 1)" in result["output"]

    def test_timeout_enforcement(self, manager: Any, jobs_root: Path) -> None:
        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        result = manager.execute_code(
            code="import time; time.sleep(30)",
            job_id=JOB_ID,
            output_dir=out_dir,
            timeout=5,
        )

        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_executor_network_enabled(
        self, tmp_path: Path, jobs_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Network access works under the default (non-airgapped) row, behind the
        restrict firewall. A user network puts the built-in DNS on loopback,
        which the firewall allows; the default bridge inherits the host's
        resolv.conf."""
        import uuid

        client = docker.from_env()
        network_name = f"openscientist-net-test-{uuid.uuid4().hex[:8]}"
        network = client.networks.create(network_name, driver="bridge")
        out_dir = jobs_root / JOB_ID / "out"
        out_dir.mkdir()
        launcher = Launcher.start(
            tmp_path,
            jobs_root,
            LAUNCHER_NETWORK=network_name,
            AGENT_IMAGE_DIGESTS=image_digest(client, EXECUTOR_IMAGE),
            EXECUTOR_IMAGE_DIGESTS=image_digest(client, EXECUTOR_IMAGE),
        )
        try:
            monkeypatch.setenv("OPENSCIENTIST_LAUNCHER_SOCKET_PATH", str(launcher.socket_path))
            clear_settings_cache()
            manager = container_manager.ContainerManager()
            # Google's generate_204: tiny, fixed 204, no rate-limiting.
            result = manager.execute_code(
                code=(
                    "import requests; "
                    'r = requests.get("https://www.google.com/generate_204", timeout=10); '
                    'print("NETWORK_OK", r.status_code)'
                ),
                job_id=JOB_ID,
                output_dir=out_dir,
            )
        finally:
            launcher.close()
            clear_settings_cache()
            network.remove()

        assert result["success"] is True
        assert "NETWORK_OK" in result["output"]

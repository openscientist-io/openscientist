"""
Tests for container_manager module.

Contains both unit tests (mocked Docker) and integration tests (real Docker).
"""

import json
import tempfile
from datetime import UTC
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest


class TestContainerManagerUnit:
    """Unit tests with mocked Docker client."""

    def test_init_defaults(self):
        """Test ContainerManager initializes with default values."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()
        assert manager.image == "openscientist-executor:latest"
        assert manager.memory_limit == "2g"
        assert manager.cpu_limit == 0.5
        assert manager.timeout == 120

    def test_init_custom_values(self):
        """Test ContainerManager accepts custom values."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager(
            image="custom:latest",
            memory_limit="4g",
            cpu_limit=1.0,
            timeout=300,
        )
        assert manager.image == "custom:latest"
        assert manager.memory_limit == "4g"
        assert manager.cpu_limit == 1.0
        assert manager.timeout == 300

    def test_execute_code_success(self):
        """Test successful code execution returns correct result."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        # Mock Docker client
        mock_client = MagicMock()
        manager._client = mock_client

        # Mock container run result
        result_json = json.dumps(
            {
                "success": True,
                "output": "Hello, World!",
                "plots": [],
                "error": None,
                "execution_time": 0.5,
            }
        )
        mock_client.containers.run.return_value = result_json.encode()

        # Mock container get for cleanup
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='print("Hello, World!")',
                job_id="test-job-123",
                output_dir=tmpdir,
            )

        assert result["success"] is True
        assert result["output"] == "Hello, World!"
        assert result["error"] is None
        mock_container.remove.assert_called_once_with(force=True)

    def test_execute_code_failure(self):
        """Test failed code execution returns error."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client

        result_json = json.dumps(
            {
                "success": False,
                "output": "",
                "plots": [],
                "error": "NameError: name 'undefined' is not defined",
                "execution_time": 0.1,
            }
        )
        mock_client.containers.run.return_value = result_json.encode()

        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code="undefined",
                job_id="test-job-456",
                output_dir=tmpdir,
            )

        assert result["success"] is False
        assert "NameError" in result["error"]

    def test_execute_code_image_not_found(self):
        """Test handling of missing executor image."""
        import docker.errors

        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.containers.run.side_effect = docker.errors.ImageNotFound("not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='print("test")',
                job_id="test-job-789",
                output_dir=tmpdir,
            )

        assert result["success"] is False
        assert "image not found" in result["error"].lower()

    def test_cleanup_job_containers(self):
        """Test cleanup of containers by job ID."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client

        # Mock list of containers for the job
        mock_container1 = MagicMock()
        mock_container1.name = "openscientist-exec-job123-abc"
        mock_container2 = MagicMock()
        mock_container2.name = "openscientist-exec-job123-def"
        mock_client.containers.list.return_value = [mock_container1, mock_container2]

        removed = manager.cleanup_job_containers("job123")

        assert removed == 2
        mock_container1.remove.assert_called_once_with(force=True)
        mock_container2.remove.assert_called_once_with(force=True)

    def test_cleanup_orphaned_containers(self):
        """Test cleanup of old orphaned containers."""
        from datetime import datetime, timedelta

        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client

        # Create mock containers - one old, one recent
        old_time = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        recent_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        old_container = MagicMock()
        old_container.name = "openscientist-exec-old"
        old_container.attrs = {"Created": old_time}

        recent_container = MagicMock()
        recent_container.name = "openscientist-exec-recent"
        recent_container.attrs = {"Created": recent_time}

        mock_client.containers.list.return_value = [old_container, recent_container]

        removed = manager.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1
        old_container.remove.assert_called_once_with(force=True)
        recent_container.remove.assert_not_called()

    def test_check_image_available_true(self):
        """Test image availability check when image exists."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.images.get.return_value = MagicMock()

        assert manager.check_image_available() is True

    def test_check_image_available_false(self):
        """Test image availability check when image doesn't exist."""
        import docker.errors

        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        assert manager.check_image_available() is False

    def test_is_available_true(self):
        """Test Docker availability check when Docker is running."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.ping.return_value = True

        assert manager.is_available() is True

    def test_is_available_false(self):
        """Test Docker availability check when Docker is not running."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.ping.side_effect = Exception("Docker not running")

        assert manager.is_available() is False


class TestBuildVolumes:
    """Unit tests for _build_volumes and _remap_paths."""

    def _make_manager(self):
        from openscientist.container_manager import ContainerManager

        return ContainerManager()

    def test_no_data_files_returns_only_output_mount(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        volumes, host_to_container = mgr._build_volumes(output_dir=tmp_path, data_files=None)
        assert str(tmp_path) in volumes
        assert volumes[str(tmp_path)] == {"bind": "/output", "mode": "rw"}
        assert host_to_container == {}

    def test_single_file_mounted_as_data(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        f = data_dir / "study.csv"
        f.write_text("a,b\n1,2\n")
        volumes, host_to_container = mgr._build_volumes(
            output_dir=tmp_path / "out",
            data_files=[{"path": str(f), "name": "study.csv"}],
        )
        assert str(data_dir) in host_to_container
        assert host_to_container[str(data_dir)] == "/data"
        assert volumes[str(data_dir)] == {"bind": "/data", "mode": "ro"}

    def test_two_files_same_dir_one_mount(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        f1 = data_dir / "a.csv"
        f2 = data_dir / "b.csv"
        f1.write_text("x\n1\n")
        f2.write_text("x\n2\n")
        volumes, host_to_container = mgr._build_volumes(
            output_dir=tmp_path / "out",
            data_files=[{"path": str(f1)}, {"path": str(f2)}],
        )
        # Both live in the same dir — only one mount
        assert len(host_to_container) == 1
        assert host_to_container[str(data_dir)] == "/data"

    def test_two_files_different_dirs_two_mounts(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        dir_a = tmp_path / "mnt_a"
        dir_b = tmp_path / "mnt_b"
        dir_a.mkdir()
        dir_b.mkdir()
        fa = dir_a / "study.csv"
        fb = dir_b / "labels.csv"
        fa.write_text("x\n1\n")
        fb.write_text("y\n2\n")
        volumes, host_to_container = mgr._build_volumes(
            output_dir=tmp_path / "out",
            data_files=[{"path": str(fa)}, {"path": str(fb)}],
        )
        assert host_to_container[str(dir_a)] == "/data"
        assert host_to_container[str(dir_b)] == "/data/1"
        assert volumes[str(dir_a)] == {"bind": "/data", "mode": "ro"}
        assert volumes[str(dir_b)] == {"bind": "/data/1", "mode": "ro"}

    def test_missing_file_raises_value_error(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        missing = tmp_path / "ghost.csv"
        with pytest.raises(ValueError, match="Data file not found"):
            mgr._build_volumes(
                output_dir=tmp_path / "out",
                data_files=[{"path": str(missing)}],
            )

    def test_empty_path_raises_value_error(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        with pytest.raises(ValueError, match="empty 'path'"):
            mgr._build_volumes(
                output_dir=tmp_path / "out",
                data_files=[{"path": ""}],
            )

    def test_remap_paths_single_dir(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        f = data_dir / "file.csv"
        f.write_text("x\n")
        host_to_container = {str(data_dir): "/data"}
        remapped_data_path, remapped_files = mgr._remap_paths(
            data_path=str(f),
            data_files=[{"path": str(f), "name": "file.csv"}],
            host_to_container=host_to_container,
        )
        assert remapped_data_path == "/data/file.csv"
        assert remapped_files[0]["path"] == "/data/file.csv"
        assert remapped_files[0]["name"] == "file.csv"  # non-path field preserved

    def test_remap_paths_multi_dir(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        fa = dir_a / "study.csv"
        fb = dir_b / "labels.csv"
        fa.write_text("x\n")
        fb.write_text("y\n")
        host_to_container = {str(dir_a): "/data", str(dir_b): "/data/1"}
        _, remapped_files = mgr._remap_paths(
            data_path=str(fa),
            data_files=[{"path": str(fa)}, {"path": str(fb)}],
            host_to_container=host_to_container,
        )
        assert remapped_files[0]["path"] == "/data/study.csv"
        assert remapped_files[1]["path"] == "/data/1/labels.csv"

    def test_remap_paths_no_map_is_identity(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        data_files = [{"path": "/host/data/f.csv"}]
        remapped_data_path, remapped_files = mgr._remap_paths(
            data_path="/host/data/f.csv",
            data_files=data_files,
            host_to_container={},
        )
        assert remapped_data_path == "/host/data/f.csv"
        assert remapped_files[0]["path"] == "/host/data/f.csv"

    def test_remap_unmounted_path_raises(self, tmp_path: Path) -> None:
        mgr = self._make_manager()
        with pytest.raises(ValueError, match="not under any mounted"):
            mgr._remap_paths(
                data_path="/some/other/dir/file.csv",
                data_files=[],
                host_to_container={"/mounted/dir": "/data"},
            )


class TestToHostPath:
    """Unit tests for the shared to_host_path function."""

    def test_path_under_container_app_dir_is_translated(self) -> None:
        from openscientist.job_container import to_host_path

        cs = MagicMock()
        cs.host_project_dir = "/host/project"
        cs.container_app_dir = "/app"

        result = to_host_path(Path("/app/jobs/123/data"), cs)
        assert result == Path("/host/project/jobs/123/data")

    def test_path_not_under_container_app_dir_unchanged(self) -> None:
        from openscientist.job_container import to_host_path

        cs = MagicMock()
        cs.host_project_dir = "/host/project"
        cs.container_app_dir = "/app"

        result = to_host_path(Path("/tmp/something"), cs)
        assert result == Path("/tmp/something")

    def test_no_host_project_dir_returns_unchanged(self) -> None:
        from openscientist.job_container import to_host_path

        cs = MagicMock()
        cs.host_project_dir = None
        cs.container_app_dir = "/app"

        result = to_host_path(Path("/app/jobs/123/data"), cs)
        assert result == Path("/app/jobs/123/data")


class TestExecutorRunConfig:
    """Unit tests for hardened executor container run configuration."""

    def _make_manager(self):
        from openscientist.container_manager import ContainerManager

        return ContainerManager()

    def _setup_execute_code(self):
        mgr = self._make_manager()
        mock_client = MagicMock()
        mgr._client = mock_client

        mock_client.containers.run.return_value = json.dumps(
            {
                "success": True,
                "output": "",
                "plots": [],
                "error": None,
                "execution_time": 0.1,
            }
        ).encode()
        mock_client.containers.get.return_value = MagicMock()

        mock_settings = MagicMock()
        mock_settings.container.container_app_dir = "/app"
        mock_settings.container.host_project_dir = None
        mock_settings.container.executor_image = "test:latest"
        mock_settings.container.executor_memory = "2g"
        mock_settings.container.executor_cpu = 0.5
        mock_settings.container.executor_timeout = 120
        return mgr, mock_client, mock_settings

    @staticmethod
    def _get_run_kwargs(mock_client: MagicMock) -> dict[Any, Any]:
        return cast(dict[Any, Any], mock_client.containers.run.call_args.kwargs)

    @staticmethod
    def _get_run_volumes(mock_client: MagicMock) -> dict[str, dict[str, str]]:
        return cast(
            dict[str, dict[str, str]], TestExecutorRunConfig._get_run_kwargs(mock_client)["volumes"]
        )

    def test_network_mode_always_none(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(code='print("hi")', job_id="test-network", output_dir=str(output_dir))

        kwargs = self._get_run_kwargs(mock_client)
        assert kwargs["network_mode"] == "none"
        assert "network" not in kwargs

    def test_read_only_root_enabled(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(code='print("hi")', job_id="test-readonly", output_dir=str(output_dir))

        assert self._get_run_kwargs(mock_client)["read_only"] is True

    def test_tmpfs_tmp_options(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(code='print("hi")', job_id="test-tmpfs", output_dir=str(output_dir))

        tmp_options = self._get_run_kwargs(mock_client)["tmpfs"]["/tmp"]
        assert "size=512m" in tmp_options
        assert "nosuid" in tmp_options
        assert "nodev" in tmp_options
        assert "mode=1777" in tmp_options
        assert "noexec" not in tmp_options

    def test_output_mount_is_rw(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(
                code='print("hi")', job_id="test-output-rw", output_dir=str(output_dir)
            )

        volumes = self._get_run_volumes(mock_client)
        output_mount = next(m for m in volumes.values() if m["bind"] == "/output")
        assert output_mount["mode"] == "rw"

    def test_data_mounts_are_ro(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        data_file = data_dir / "file.csv"
        data_file.write_text("a,b\n1,2\n")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(
                code='print("hi")',
                job_id="test-data-ro",
                output_dir=str(output_dir),
                data_files=[{"path": str(data_file), "name": "file.csv"}],
            )

        volumes = self._get_run_volumes(mock_client)
        data_mounts = [m for m in volumes.values() if m["bind"].startswith("/data")]
        assert data_mounts
        assert all(m["mode"] == "ro" for m in data_mounts)

    def test_no_environment_passed(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(code='print("hi")', job_id="test-no-env", output_dir=str(output_dir))

        assert "environment" not in self._get_run_kwargs(mock_client)

    def test_resource_limits_preserved(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from openscientist.container_manager import ContainerManager

        mgr = ContainerManager(memory_limit="4g", cpu_limit=1.25)
        mock_client = MagicMock()
        mgr._client = mock_client
        mock_client.containers.run.return_value = json.dumps(
            {"success": True, "output": "", "plots": [], "error": None, "execution_time": 0.1}
        ).encode()
        mock_client.containers.get.return_value = MagicMock()

        mock_settings = MagicMock()
        mock_settings.container.container_app_dir = "/app"
        mock_settings.container.host_project_dir = None

        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(code='print("hi")', job_id="test-limits", output_dir=str(output_dir))

        kwargs = self._get_run_kwargs(mock_client)
        assert kwargs["mem_limit"] == "4g"
        assert kwargs["nano_cpus"] == int(1.25 * 1e9)

    def test_security_settings_preserved(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            mgr.execute_code(code='print("hi")', job_id="job-secure", output_dir=str(output_dir))

        kwargs = self._get_run_kwargs(mock_client)
        assert kwargs["user"] == "executor"
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        assert kwargs["stop_signal"] == "SIGKILL"
        assert kwargs["labels"]["openscientist.job_id"] == "job-secure"
        assert kwargs["labels"]["openscientist.type"] == "executor"


class TestBuildVolumesHostTranslation:
    """Tests for host path translation in _build_volumes via execute_code."""

    def _make_manager(self):
        from openscientist.container_manager import ContainerManager

        return ContainerManager()

    def _setup_execute_code(self):
        mgr = self._make_manager()
        mock_client = MagicMock()
        mgr._client = mock_client

        mock_client.containers.run.return_value = json.dumps(
            {
                "success": True,
                "output": "",
                "plots": [],
                "error": None,
                "execution_time": 0.1,
            }
        ).encode()
        mock_client.containers.get.return_value = MagicMock()

        mock_settings = MagicMock()
        mock_settings.container.container_app_dir = "/app"
        mock_settings.container.executor_image = "test:latest"
        mock_settings.container.executor_memory = "2g"
        mock_settings.container.executor_cpu = 0.5
        mock_settings.container.executor_timeout = 120
        mock_settings.container.agent_network = None
        mock_settings.airgap.enabled = False
        return mgr, mock_client, mock_settings

    @staticmethod
    def _get_run_volumes(mock_client: MagicMock) -> dict[str, dict[str, str]]:
        call_kwargs = mock_client.containers.run.call_args
        volumes = cast(dict[str, dict[str, str]] | None, call_kwargs.kwargs.get("volumes"))
        if volumes is not None:
            return volumes
        return cast(dict[str, dict[str, str]], call_kwargs[1]["volumes"])

    def test_build_volumes_translates_output_dir(self, tmp_path: Path) -> None:
        """When host_project_dir is set, output_dir is translated."""
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        mock_settings.container.host_project_dir = "/host/project"

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            # Use an output_dir under /app to trigger translation
            output_dir = tmp_path / "provenance"
            output_dir.mkdir()

            # Patch to_host_path to simulate translation
            with patch("openscientist.container_manager.to_host_path") as mock_thp:
                mock_thp.return_value = Path("/host/project/jobs/123/provenance")
                mgr.execute_code(
                    code='print("hi")',
                    job_id="test-translate",
                    output_dir=str(output_dir),
                )
                mock_thp.assert_called()

            assert "/host/project/jobs/123/provenance" in self._get_run_volumes(mock_client)

    def test_build_volumes_translates_data_file_paths(self, tmp_path: Path) -> None:
        """When host_project_dir is set, data file paths are translated."""
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        mock_settings.container.host_project_dir = "/host/project"

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        data_file = data_dir / "file.csv"
        data_file.write_text("a,b\n1,2\n")

        host_data_dir = tmp_path / "host_data"
        host_data_dir.mkdir()
        (host_data_dir / "file.csv").write_text("a,b\n1,2\n")

        def mock_to_host_path(path, cs):
            try:
                rel = path.relative_to(data_dir)
                return host_data_dir / rel
            except ValueError:
                pass
            return path

        with (
            patch("openscientist.container_manager.get_settings", return_value=mock_settings),
            patch("openscientist.container_manager.to_host_path", side_effect=mock_to_host_path),
        ):
            output_dir = tmp_path / "out"
            output_dir.mkdir()
            mgr.execute_code(
                code='print("hi")',
                job_id="test-data-translate",
                output_dir=str(output_dir),
                data_files=[{"path": str(data_file), "name": "file.csv"}],
            )

        assert str(host_data_dir) in self._get_run_volumes(mock_client)

    def test_build_volumes_no_translation_without_host_dir(self, tmp_path: Path) -> None:
        """When host_project_dir is not set, paths pass through unchanged."""
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup_execute_code()
        mock_settings.container.host_project_dir = None

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        data_file = data_dir / "file.csv"
        data_file.write_text("a,b\n1,2\n")

        with patch("openscientist.container_manager.get_settings", return_value=mock_settings):
            output_dir = tmp_path / "out"
            output_dir.mkdir()
            mgr.execute_code(
                code='print("hi")',
                job_id="test-no-translate",
                output_dir=str(output_dir),
                data_files=[{"path": str(data_file), "name": "file.csv"}],
            )

        assert str(data_dir) in self._get_run_volumes(mock_client)


class TestExecuteCodeAirgapNetwork:
    """The executor has no network, with or without the air-gap flag."""

    def _setup(self):
        from openscientist.container_manager import ContainerManager

        mgr = ContainerManager()
        mock_client = MagicMock()
        mgr._client = mock_client
        mock_client.containers.run.return_value = json.dumps(
            {
                "success": True,
                "output": "",
                "plots": [],
                "error": None,
                "execution_time": 0.1,
            }
        ).encode()
        mock_client.containers.get.return_value = MagicMock()

        mock_settings = MagicMock()
        mock_settings.container.container_app_dir = "/app"
        mock_settings.container.host_project_dir = None
        mock_settings.container.executor_image = "test:latest"
        mock_settings.container.executor_memory = "2g"
        mock_settings.container.executor_cpu = 0.5
        mock_settings.container.executor_timeout = 120
        mock_settings.container.agent_network = "openscientist_default"
        return mgr, mock_client, mock_settings

    @staticmethod
    def _run_kwargs(mock_client: MagicMock) -> dict[str, object]:
        return cast(dict[str, object], mock_client.containers.run.call_args.kwargs)

    def test_airgap_enabled_uses_no_network(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup()
        mock_settings.airgap.enabled = True

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        with (
            patch("openscientist.container_manager.get_settings", return_value=mock_settings),
            patch(
                "openscientist.job_container.resolve_docker_network",
                return_value="openscientist_default",
            ) as mock_resolve,
        ):
            mgr.execute_code(
                code='print("hi")',
                job_id="airgap-on",
                output_dir=str(output_dir),
            )

        kwargs = self._run_kwargs(mock_client)
        # This fork pins the executor to no networking unconditionally
        # (16d97c9 "security: harden executor container isolation"), so the
        # airgap flag does not change the outcome here.
        assert kwargs.get("network_mode") == "none"
        assert "network" not in kwargs
        mock_resolve.assert_not_called()

    def test_airgap_disabled_uses_resolved_network(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        mgr, mock_client, mock_settings = self._setup()
        mock_settings.airgap.enabled = False

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        with (
            patch("openscientist.container_manager.get_settings", return_value=mock_settings),
            patch(
                "openscientist.job_container.resolve_docker_network",
                return_value="openscientist_default",
            ) as mock_resolve,
        ):
            mgr.execute_code(
                code='print("hi")',
                job_id="airgap-off",
                output_dir=str(output_dir),
            )

        kwargs = self._run_kwargs(mock_client)
        # Still no networking: the executor runs untrusted code and is isolated
        # regardless of the airgap flag. See the note above.
        assert kwargs.get("network_mode") == "none"
        assert "network" not in kwargs
        # No compose network is resolved: there is nothing to attach to.
        mock_resolve.assert_not_called()


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
class TestContainerManagerIntegration:
    """Integration tests that require Docker to be running.

    Run with: pytest tests/test_container_manager.py -v -m integration
    """

    @pytest.fixture(autouse=True)
    def check_docker(self):
        """Skip if Docker is not available."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()
        if not manager.is_available():
            pytest.skip("Docker not available")

    @pytest.fixture(autouse=True)
    def check_image(self):
        """Skip if executor image is not built."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()
        if not manager.check_image_available():
            pytest.skip("Executor image not built. Run 'make build-executor' first.")

    def test_execute_simple_code(self):
        """Test executing simple Python code."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='print("Hello from container!")',
                job_id="integration-test-1",
                output_dir=tmpdir,
            )

        assert result["success"] is True
        assert "Hello from container!" in result["output"]

    def test_execute_with_numpy(self):
        """Test that numpy is available."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code="import numpy as np; print(np.array([1,2,3]).sum())",
                job_id="integration-test-2",
                output_dir=tmpdir,
            )

        assert result["success"] is True
        assert "6" in result["output"]

    def test_execute_with_pandas(self):
        """Test that pandas is available."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='import pandas as pd; df = pd.DataFrame({"a": [1,2,3]}); print(df.shape)',
                job_id="integration-test-3",
                output_dir=tmpdir,
            )

        assert result["success"] is True
        assert "(3, 1)" in result["output"]

    def test_network_isolated(self):
        """Executor image with network_mode=none must block outbound connections."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager()
        probe_script = """
import socket
try:
    socket.create_connection(("1.1.1.1", 80), timeout=3)
    print("NETWORK_OK")
except OSError:
    print("NETWORK_BLOCKED")
"""
        output = manager.client.containers.run(
            image=manager.image,
            network_mode="none",
            user="executor",
            entrypoint=[],
            remove=True,
            command=["python", "-c", probe_script.strip()],
        )

        text = output.decode("utf-8") if isinstance(output, bytes) else output
        assert "NETWORK_BLOCKED" in text
        assert "NETWORK_OK" not in text

    def test_timeout_enforcement(self):
        """Test that timeout is enforced."""
        from openscientist.container_manager import ContainerManager

        manager = ContainerManager(timeout=5)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code="import time; time.sleep(30)",
                job_id="integration-test-5",
                output_dir=tmpdir,
                timeout=5,
            )

        # Should fail due to timeout
        assert result["success"] is False
        assert "timed out" in result["error"].lower()

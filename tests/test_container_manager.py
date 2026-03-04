"""
Tests for container_manager module.

Contains both unit tests (mocked Docker) and integration tests (real Docker).
"""

import json
import tempfile
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestContainerManagerUnit:
    """Unit tests with mocked Docker client."""

    def test_init_defaults(self):
        """Test ContainerManager initializes with default values."""
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()
        assert manager.image == "open_scientist-executor:latest"
        assert manager.memory_limit == "2g"
        assert manager.cpu_limit == 0.5
        assert manager.timeout == 120

    def test_init_custom_values(self):
        """Test ContainerManager accepts custom values."""
        from open_scientist.container_manager import ContainerManager

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
        from open_scientist.container_manager import ContainerManager

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
        from open_scientist.container_manager import ContainerManager

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

        from open_scientist.container_manager import ContainerManager

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
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client

        # Mock list of containers for the job
        mock_container1 = MagicMock()
        mock_container1.name = "open_scientist-exec-job123-abc"
        mock_container2 = MagicMock()
        mock_container2.name = "open_scientist-exec-job123-def"
        mock_client.containers.list.return_value = [mock_container1, mock_container2]

        removed = manager.cleanup_job_containers("job123")

        assert removed == 2
        mock_container1.remove.assert_called_once_with(force=True)
        mock_container2.remove.assert_called_once_with(force=True)

    def test_cleanup_orphaned_containers(self):
        """Test cleanup of old orphaned containers."""
        from datetime import datetime, timedelta

        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client

        # Create mock containers - one old, one recent
        old_time = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        recent_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        old_container = MagicMock()
        old_container.name = "open_scientist-exec-old"
        old_container.attrs = {"Created": old_time}

        recent_container = MagicMock()
        recent_container.name = "open_scientist-exec-recent"
        recent_container.attrs = {"Created": recent_time}

        mock_client.containers.list.return_value = [old_container, recent_container]

        removed = manager.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1
        old_container.remove.assert_called_once_with(force=True)
        recent_container.remove.assert_not_called()

    def test_check_image_available_true(self):
        """Test image availability check when image exists."""
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.images.get.return_value = MagicMock()

        assert manager.check_image_available() is True

    def test_check_image_available_false(self):
        """Test image availability check when image doesn't exist."""
        import docker.errors

        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        assert manager.check_image_available() is False

    def test_is_available_true(self):
        """Test Docker availability check when Docker is running."""
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.ping.return_value = True

        assert manager.is_available() is True

    def test_is_available_false(self):
        """Test Docker availability check when Docker is not running."""
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.ping.side_effect = Exception("Docker not running")

        assert manager.is_available() is False


class TestBuildVolumes:
    """Unit tests for _build_volumes and _remap_paths."""

    def _make_manager(self):
        from open_scientist.container_manager import ContainerManager

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


class TestGetContainerManager:
    """Tests for the global container manager singleton."""

    def test_get_container_manager_returns_same_instance(self):
        """Test that get_container_manager returns a singleton."""
        from open_scientist import container_manager

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
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()
        if not manager.is_available():
            pytest.skip("Docker not available")

    @pytest.fixture(autouse=True)
    def check_image(self):
        """Skip if executor image is not built."""
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()
        if not manager.check_image_available():
            pytest.skip("Executor image not built. Run 'make build-executor' first.")

    def test_execute_simple_code(self):
        """Test executing simple Python code."""
        from open_scientist.container_manager import ContainerManager

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
        from open_scientist.container_manager import ContainerManager

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
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='import pandas as pd; df = pd.DataFrame({"a": [1,2,3]}); print(df.shape)',
                job_id="integration-test-3",
                output_dir=tmpdir,
            )

        assert result["success"] is True
        assert "(3, 1)" in result["output"]

    def test_network_enabled(self):
        """Test that network access is available (needed for requests, SPARQL, cargo, etc.)."""
        from open_scientist.container_manager import ContainerManager

        manager = ContainerManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='import requests; r = requests.get("https://httpbin.org/get", timeout=5); print(r.status_code)',
                job_id="integration-test-4",
                output_dir=tmpdir,
            )

        assert result["success"] is True
        assert "200" in result["output"]

    def test_timeout_enforcement(self):
        """Test that timeout is enforced."""
        from open_scientist.container_manager import ContainerManager

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

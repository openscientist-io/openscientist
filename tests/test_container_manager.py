"""
Tests for container_manager module.

Contains both unit tests (mocked Docker) and integration tests (real Docker).
"""

import json
import tempfile
from unittest.mock import MagicMock

import pytest


class TestContainerManagerUnit:
    """Unit tests with mocked Docker client."""

    def test_init_defaults(self):
        """Test ContainerManager initializes with default values."""
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()
        assert manager.image == "shandy-executor:latest"
        assert manager.memory_limit == "2g"
        assert manager.cpu_limit == 0.5
        assert manager.timeout == 120

    def test_init_custom_values(self):
        """Test ContainerManager accepts custom values."""
        from shandy.container_manager import ContainerManager

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
        from shandy.container_manager import ContainerManager

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
        from shandy.container_manager import ContainerManager

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

        from shandy.container_manager import ContainerManager

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
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client

        # Mock list of containers for the job
        mock_container1 = MagicMock()
        mock_container1.name = "shandy-exec-job123-abc"
        mock_container2 = MagicMock()
        mock_container2.name = "shandy-exec-job123-def"
        mock_client.containers.list.return_value = [mock_container1, mock_container2]

        removed = manager.cleanup_job_containers("job123")

        assert removed == 2
        mock_container1.remove.assert_called_once_with(force=True)
        mock_container2.remove.assert_called_once_with(force=True)

    def test_cleanup_orphaned_containers(self):
        """Test cleanup of old orphaned containers."""
        from datetime import datetime, timedelta, timezone

        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client

        # Create mock containers - one old, one recent
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        old_container = MagicMock()
        old_container.name = "shandy-exec-old"
        old_container.attrs = {"Created": old_time}

        recent_container = MagicMock()
        recent_container.name = "shandy-exec-recent"
        recent_container.attrs = {"Created": recent_time}

        mock_client.containers.list.return_value = [old_container, recent_container]

        removed = manager.cleanup_orphaned_containers(max_age_hours=24)

        assert removed == 1
        old_container.remove.assert_called_once_with(force=True)
        recent_container.remove.assert_not_called()

    def test_check_image_available_true(self):
        """Test image availability check when image exists."""
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.images.get.return_value = MagicMock()

        assert manager.check_image_available() is True

    def test_check_image_available_false(self):
        """Test image availability check when image doesn't exist."""
        import docker.errors

        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        assert manager.check_image_available() is False

    def test_is_available_true(self):
        """Test Docker availability check when Docker is running."""
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.ping.return_value = True

        assert manager.is_available() is True

    def test_is_available_false(self):
        """Test Docker availability check when Docker is not running."""
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        mock_client = MagicMock()
        manager._client = mock_client
        mock_client.ping.side_effect = Exception("Docker not running")

        assert manager.is_available() is False


class TestGetContainerManager:
    """Tests for the global container manager singleton."""

    def test_get_container_manager_returns_same_instance(self):
        """Test that get_container_manager returns a singleton."""
        from shandy import container_manager

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
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()
        if not manager.is_available():
            pytest.skip("Docker not available")

    @pytest.fixture(autouse=True)
    def check_image(self):
        """Skip if executor image is not built."""
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()
        if not manager.check_image_available():
            pytest.skip("Executor image not built. Run 'make build-executor' first.")

    def test_execute_simple_code(self):
        """Test executing simple Python code."""
        from shandy.container_manager import ContainerManager

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
        from shandy.container_manager import ContainerManager

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
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='import pandas as pd; df = pd.DataFrame({"a": [1,2,3]}); print(df.shape)',
                job_id="integration-test-3",
                output_dir=tmpdir,
            )

        assert result["success"] is True
        assert "(3, 1)" in result["output"]

    def test_network_disabled(self):
        """Test that network access is blocked."""
        from shandy.container_manager import ContainerManager

        manager = ContainerManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = manager.execute_code(
                code='import requests; requests.get("https://google.com")',
                job_id="integration-test-4",
                output_dir=tmpdir,
            )

        # Should fail due to network being disabled
        assert result["success"] is False

    def test_timeout_enforcement(self):
        """Test that timeout is enforced."""
        from shandy.container_manager import ContainerManager

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

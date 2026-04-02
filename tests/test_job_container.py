"""Tests for openscientist.job_container module."""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from openscientist.job_container.runner import AGENT_APP_DIR, JobContainerRunner


class TestJobContainerRunner:
    """Tests for JobContainerRunner."""

    @staticmethod
    def _make_settings(*, host_project_dir: str | None) -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {"EXTRA_ENV": "1"}
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=host_project_dir,
                container_app_dir="/app",
                agent_network=None,
                agent_memory="8g",
                agent_cpu=2.0,
            ),
            provider=provider,
            database=SimpleNamespace(effective_database_url="postgresql://db"),
            secret_key="secret",
        )

    def test_docker_unavailable_raises(self):
        mock_docker = MagicMock()
        mock_docker.from_env.side_effect = Exception("Docker not running")

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with pytest.raises(Exception, match="Docker not running"):
                JobContainerRunner()

    def test_docker_available(self):
        mock_client = MagicMock()
        mock_docker = MagicMock()
        mock_docker.from_env.return_value = mock_client

        with patch.dict(sys.modules, {"docker": mock_docker}):
            runner = JobContainerRunner()
            assert runner._docker is mock_client

    def test_launch_passes_host_path_mapping_to_agent_container(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        mock_docker = MagicMock()
        mock_docker.from_env.return_value = mock_client
        settings = self._make_settings(host_project_dir="/host/project")

        original_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            if path == Path("/var/run/docker.sock"):
                return False
            return cast(bool, original_exists(path))

        with (
            patch.dict(sys.modules, {"docker": mock_docker}),
            patch("openscientist.settings.get_settings", return_value=settings),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.to_host_path",
                return_value=Path("/host/project/jobs/job-123"),
            ),
            patch.object(Path, "exists", autospec=True, side_effect=fake_exists),
        ):
            runner = JobContainerRunner()
            runner.launch("job-123", Path("/app/jobs/job-123"))

        run_kwargs = mock_client.containers.run.call_args.kwargs
        environment = run_kwargs["environment"]
        assert environment["JOB_DIR"] == f"{AGENT_APP_DIR}/jobs/job-123"
        assert environment["OPENSCIENTIST_HOST_PROJECT_DIR"] == "/host/project"
        assert environment["OPENSCIENTIST_CONTAINER_APP_DIR"] == AGENT_APP_DIR
        assert run_kwargs["volumes"]["/host/project/jobs/job-123"]["bind"] == environment["JOB_DIR"]

    def test_launch_omits_host_path_mapping_without_host_project_dir(self):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        mock_docker = MagicMock()
        mock_docker.from_env.return_value = mock_client
        settings = self._make_settings(host_project_dir=None)

        original_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            if path == Path("/var/run/docker.sock"):
                return False
            return cast(bool, original_exists(path))

        with (
            patch.dict(sys.modules, {"docker": mock_docker}),
            patch("openscientist.settings.get_settings", return_value=settings),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.to_host_path", return_value=Path("/app/jobs/job-123")
            ),
            patch.object(Path, "exists", autospec=True, side_effect=fake_exists),
        ):
            runner = JobContainerRunner()
            runner.launch("job-123", Path("/app/jobs/job-123"))

        environment = mock_client.containers.run.call_args.kwargs["environment"]
        assert "OPENSCIENTIST_HOST_PROJECT_DIR" not in environment
        assert "OPENSCIENTIST_CONTAINER_APP_DIR" not in environment

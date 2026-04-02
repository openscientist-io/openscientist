"""Tests for openscientist.job_container module."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from openscientist.job_container.runner import JobContainerRunner


class TestJobContainerRunner:
    """Tests for JobContainerRunner init."""

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


class TestPhenixMount:
    """Tests for Phenix volume mount in agent containers."""

    def _make_runner(self) -> JobContainerRunner:
        mock_docker = MagicMock()
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        with patch.dict(sys.modules, {"docker": mock_docker}):
            return JobContainerRunner()

    def _mock_settings(
        self,
        *,
        phenix_available: bool,
        phenix_path: str | None = None,
        phenix_host_path: str | None = None,
    ) -> MagicMock:
        settings = MagicMock()
        settings.container.host_project_dir = None
        settings.container.container_app_dir = "/app"
        settings.container.agent_network = None
        settings.container.agent_memory = "8g"
        settings.container.agent_cpu = 2.0
        settings.secret_key = "test-secret"
        settings.database.effective_database_url = "postgresql+asyncpg://test"
        settings.provider.get_container_env_vars.return_value = {}
        settings.provider.google_application_credentials = None

        # Phenix settings
        phenix = MagicMock()
        type(phenix).is_available = PropertyMock(return_value=phenix_available)
        phenix.phenix_path = phenix_path
        phenix.phenix_host_path = phenix_host_path
        settings.phenix = phenix

        return settings

    @patch("openscientist.job_container.runner.os.stat")
    @patch("openscientist.job_container.resolve_docker_network", return_value="bridge")
    @patch("openscientist.settings.get_settings")
    def test_phenix_mounted_when_available(self, mock_get_settings, _net, mock_stat):
        mock_stat.return_value = MagicMock(st_gid=999)
        settings = self._mock_settings(
            phenix_available=True,
            phenix_path="/opt/phenix",
            phenix_host_path="/Applications/phenix-1.21.2",
        )
        mock_get_settings.return_value = settings

        runner = self._make_runner()
        job_dir = Path("/app/jobs/test-job-id")

        with patch.object(Path, "exists", return_value=True):
            runner.launch("test-job-id", job_dir)

        call_kwargs = runner._docker.containers.run.call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        assert "/Applications/phenix-1.21.2" in volumes
        assert volumes["/Applications/phenix-1.21.2"] == {"bind": "/opt/phenix", "mode": "ro"}
        assert env["PHENIX_PATH"] == "/opt/phenix"

    @patch("openscientist.job_container.runner.os.stat")
    @patch("openscientist.job_container.resolve_docker_network", return_value="bridge")
    @patch("openscientist.settings.get_settings")
    def test_phenix_not_mounted_without_host_path(self, mock_get_settings, _net, mock_stat):
        """Phenix is NOT mounted when phenix_host_path is unset, even if phenix_path is set.

        This prevents macOS Phenix binaries from being mounted into Linux containers.
        """
        mock_stat.return_value = MagicMock(st_gid=999)
        settings = self._mock_settings(
            phenix_available=True,
            phenix_path="/Applications/phenix-1.21.2",
            phenix_host_path=None,
        )
        mock_get_settings.return_value = settings

        runner = self._make_runner()
        job_dir = Path("/app/jobs/test-job-id")

        with patch.object(Path, "exists", return_value=True):
            runner.launch("test-job-id", job_dir)

        call_kwargs = runner._docker.containers.run.call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        for key in volumes:
            assert "phenix" not in key.lower()
        assert "PHENIX_PATH" not in env

    @patch("openscientist.job_container.runner.os.stat")
    @patch("openscientist.job_container.resolve_docker_network", return_value="bridge")
    @patch("openscientist.settings.get_settings")
    def test_phenix_not_mounted_when_unavailable(self, mock_get_settings, _net, mock_stat):
        mock_stat.return_value = MagicMock(st_gid=999)
        settings = self._mock_settings(phenix_available=False)
        mock_get_settings.return_value = settings

        runner = self._make_runner()
        job_dir = Path("/app/jobs/test-job-id")

        with patch.object(Path, "exists", return_value=True):
            runner.launch("test-job-id", job_dir)

        call_kwargs = runner._docker.containers.run.call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        # No phenix-related volume
        for key in volumes:
            assert "phenix" not in key.lower()
        assert "PHENIX_PATH" not in env

"""Tests for openscientist.job_container module."""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, PropertyMock, patch

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
                agent_platform=None,
            ),
            provider=provider,
            database=SimpleNamespace(effective_database_url="postgresql://db"),
            phenix=SimpleNamespace(phenix_host_path=None),
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
        """Phenix is not mounted when phenix_host_path is unset."""
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

        for key in volumes:
            assert "phenix" not in key.lower()
        assert "PHENIX_PATH" not in env

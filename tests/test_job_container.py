"""Tests for openscientist.job_container module."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from docker import errors as docker_errors
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
        """Runner construction surfaces Docker startup failures."""
        with patch(
            "openscientist.job_container.runner.docker.from_env",
            side_effect=Exception("Docker not running"),
        ):
            with pytest.raises(Exception, match="Docker not running"):
                JobContainerRunner()

    def test_docker_available(self):
        """Runner construction stores the Docker client from ``from_env``."""
        mock_client = MagicMock()

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            assert vars(runner)["_docker"] is mock_client

    def test_launch_passes_host_path_mapping_to_agent_container(self):
        """Launch passes the translated job mount and host mapping to the agent."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        settings = self._make_settings(host_project_dir="/host/project")

        original_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            if path == Path("/var/run/docker.sock"):
                return False
            return cast(bool, original_exists(path))

        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.get_settings", return_value=settings),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.runner.to_host_path",
                return_value=Path("/host/project/jobs/job-123"),
            ),
            patch.object(Path, "exists", autospec=True, side_effect=fake_exists),
        ):
            runner = JobContainerRunner()
            runner.launch("job-123", Path("/app/jobs/job-123"))

        run_kwargs = cast(MagicMock, mock_client.containers.run).call_args.kwargs
        environment = run_kwargs["environment"]
        assert environment["JOB_DIR"] == f"{AGENT_APP_DIR}/jobs/job-123"
        assert environment["OPENSCIENTIST_HOST_PROJECT_DIR"] == "/host/project"
        assert environment["OPENSCIENTIST_CONTAINER_APP_DIR"] == AGENT_APP_DIR
        assert run_kwargs["volumes"]["/host/project/jobs/job-123"]["bind"] == environment["JOB_DIR"]

    def test_launch_omits_host_path_mapping_without_host_project_dir(self):
        """Launch omits host-path env vars when the host project path is unset."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        settings = self._make_settings(host_project_dir=None)

        original_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            if path == Path("/var/run/docker.sock"):
                return False
            return cast(bool, original_exists(path))

        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.get_settings", return_value=settings),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.runner.to_host_path",
                return_value=Path("/app/jobs/job-123"),
            ),
            patch.object(Path, "exists", autospec=True, side_effect=fake_exists),
        ):
            runner = JobContainerRunner()
            runner.launch("job-123", Path("/app/jobs/job-123"))

        environment = cast(MagicMock, mock_client.containers.run).call_args.kwargs["environment"]
        assert "OPENSCIENTIST_HOST_PROJECT_DIR" not in environment
        assert "OPENSCIENTIST_CONTAINER_APP_DIR" not in environment

    def test_get_exit_code_looks_up_agent_container_by_labels(self):
        """Exit-code polling filters to the agent container, not job executors."""
        mock_client = MagicMock()
        mock_agent = MagicMock()
        mock_agent.status = "running"
        mock_client.containers.list.return_value = [mock_agent]

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            assert runner.get_exit_code("job-123") is None

        mock_client.containers.list.assert_called_once_with(
            all=True,
            filters={
                "label": [
                    "openscientist.job_id=job-123",
                    "openscientist.type=agent",
                ]
            },
        )

    def test_get_exit_code_returns_none_when_container_disappears(self):
        """Exit-code polling treats a vanished container as a benign miss."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.reload.side_effect = docker_errors.NotFound("gone")
        mock_client.containers.list.return_value = [mock_container]

        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.logger.warning") as mock_warning,
        ):
            runner = JobContainerRunner()
            exit_code = runner.get_exit_code("job-123")

        assert exit_code is None
        mock_warning.assert_not_called()


class TestPhenixMount:
    """Tests for Phenix volume mount in agent containers."""

    def _make_runner(self) -> tuple[JobContainerRunner, MagicMock]:
        """Construct a runner with a mocked Docker client."""
        mock_client = MagicMock()
        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            return JobContainerRunner(), mock_client

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
    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_phenix_mounted_when_available(self, mock_get_settings, _net, mock_stat):
        """The configured Linux Phenix path is mounted into the agent container."""
        mock_stat.return_value = MagicMock(st_gid=999)
        settings = self._mock_settings(
            phenix_available=True,
            phenix_path="/opt/phenix",
            phenix_host_path="/Applications/phenix-1.21.2",
        )
        mock_get_settings.return_value = settings

        runner, mock_client = self._make_runner()
        job_dir = Path("/app/jobs/test-job-id")

        with patch.object(Path, "exists", return_value=True):
            runner.launch("test-job-id", job_dir)

        call_kwargs = cast(MagicMock, mock_client.containers.run).call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        assert "/Applications/phenix-1.21.2" in volumes
        assert volumes["/Applications/phenix-1.21.2"] == {"bind": "/opt/phenix", "mode": "ro"}
        assert env["PHENIX_PATH"] == "/opt/phenix"

    @patch("openscientist.job_container.runner.os.stat")
    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_phenix_not_mounted_without_host_path(self, mock_get_settings, _net, mock_stat):
        """Phenix is not mounted when phenix_host_path is unset."""
        mock_stat.return_value = MagicMock(st_gid=999)
        settings = self._mock_settings(
            phenix_available=True,
            phenix_path="/Applications/phenix-1.21.2",
            phenix_host_path=None,
        )
        mock_get_settings.return_value = settings

        runner, mock_client = self._make_runner()
        job_dir = Path("/app/jobs/test-job-id")

        with patch.object(Path, "exists", return_value=True):
            runner.launch("test-job-id", job_dir)

        call_kwargs = cast(MagicMock, mock_client.containers.run).call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        for key in volumes:
            assert "phenix" not in key.lower()
        assert "PHENIX_PATH" not in env

    @patch("openscientist.job_container.runner.os.stat")
    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_phenix_not_mounted_when_unavailable(self, mock_get_settings, _net, mock_stat):
        """Phenix mounts are omitted when the feature is unavailable."""
        mock_stat.return_value = MagicMock(st_gid=999)
        settings = self._mock_settings(phenix_available=False)
        mock_get_settings.return_value = settings

        runner, mock_client = self._make_runner()
        job_dir = Path("/app/jobs/test-job-id")

        with patch.object(Path, "exists", return_value=True):
            runner.launch("test-job-id", job_dir)

        call_kwargs = cast(MagicMock, mock_client.containers.run).call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        for key in volumes:
            assert "phenix" not in key.lower()
        assert "PHENIX_PATH" not in env

"""Tests for openscientist.job_container module."""

import hashlib
import hmac
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from docker import errors as docker_errors
from openscientist.job.types import RunMode
from openscientist.job_container.runner import (
    AGENT_APP_DIR,
    AGENT_GCP_CREDENTIALS_PATH,
    JobContainerRunner,
)
from openscientist.job_container.secrets import derive_job_secret, make_exec_placeholder
from openscientist.providers.base import AirgapEgress, AirgapPosture
from openscientist.settings import Settings


class _FakeProvider:
    def __init__(self, posture: AirgapPosture | None = None) -> None:
        self._posture = posture

    def proxied_container_env(
        self,
        *,
        proxy_base_url: str,
        placeholder: str,
        gcp_credentials_container_path: str | None = None,
    ) -> dict[str, str]:
        env = {"EXTRA_ENV": "1"}
        if gcp_credentials_container_path:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = gcp_credentials_container_path
        return env

    def prelaunch_model_context_env(self) -> dict[str, str]:
        return {}

    def airgap_egress(self) -> AirgapPosture:
        return self._posture or AirgapPosture(AirgapEgress.PROXY)


@pytest.fixture(autouse=True)
def _fake_provider():
    with patch("openscientist.job_container.runner.get_provider", return_value=_FakeProvider()):
        yield


class TestJobContainerRunner:
    """Tests for JobContainerRunner."""

    @staticmethod
    def _make_settings(
        *,
        host_project_dir: str | None,
        agent_image: str = "openscientist-agent:latest",
    ) -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {"EXTRA_ENV": "1"}
        provider.codex_auth_host_path = None
        provider.gcp_credentials_host_path = None
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=host_project_dir,
                container_app_dir="/app",
                agent_network=None,
                agent_memory="8g",
                agent_cpu=2.0,
                agent_platform=None,
                agent_image=agent_image,
            ),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql://db",
                effective_admin_database_url="postgresql://admin-db",
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            airgap=SimpleNamespace(enabled=False),
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

    def test_build_container_volumes_uses_posix_host_keys(self):
        """Docker volume host keys use forward slashes on all platforms."""
        settings = self._make_settings(host_project_dir="/host/project")
        settings.provider.google_application_credentials = "C:/creds/gcp.json"
        settings.provider.gcp_credentials_host_path = "C:/creds/gcp.json"
        settings.phenix = SimpleNamespace(phenix_host_path="/opt/host-phenix")

        job_dir_host = Path("/host/project/jobs/job-123")
        with patch.object(Path, "resolve", lambda self: self):
            volumes = JobContainerRunner._build_container_volumes(
                cast(Settings, settings),
                job_dir_host=job_dir_host,
                job_mount=f"{AGENT_APP_DIR}/jobs/job-123",
            )

        assert volumes["/host/project/jobs/job-123"] == {
            "bind": f"{AGENT_APP_DIR}/jobs/job-123",
            "mode": "rw",
        }
        assert volumes["C:/creds/gcp.json"] == {
            "bind": "/agent/gcp-credentials.json",
            "mode": "ro",
        }
        assert volumes["/opt/host-phenix"] == {"bind": "/opt/phenix", "mode": "ro"}
        for host_key in volumes:
            assert "\\" not in host_key

    def test_launch_omits_docker_socket_and_group_add(self):
        """The job container no longer mounts the Docker socket or joins its group."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        settings = self._make_settings(host_project_dir="/host/project")

        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.get_settings", return_value=settings),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.runner.to_host_path",
                return_value=Path("/host/project/jobs/job-123"),
            ),
        ):
            runner = JobContainerRunner()
            runner.launch("job-123", Path("/app/jobs/job-123"))

        run_kwargs = cast(MagicMock, mock_client.containers.run).call_args.kwargs
        assert "/var/run/docker.sock" not in run_kwargs["volumes"]
        assert "group_add" not in run_kwargs

    def test_launch_uses_agent_image_from_settings(self):
        """Launch passes the configured agent_image to containers.run.

        Regression test for #132: hardcoded :latest tag prevented staging
        deployments from isolating their agent image from prod's :latest.
        """
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        settings = self._make_settings(
            host_project_dir=None,
            agent_image="openscientist-agent:staging",
        )

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

        run_kwargs = cast(MagicMock, mock_client.containers.run).call_args.kwargs
        assert run_kwargs["image"] == "openscientist-agent:staging"

    def test_launch_maps_host_docker_internal_to_gateway(self):
        """The agent container maps host.docker.internal to the host gateway so a
        job can reach a model server running on the host (e.g. a local Ollama)."""
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

        run_kwargs = cast(MagicMock, mock_client.containers.run).call_args.kwargs
        assert run_kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}

    def test_launch_inherits_docker_host_for_proxy(self):
        """The agent inherits the web app's DOCKER_HOST verbatim, so its
        ContainerManager talks to the same restricted socket proxy (R6)."""
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://custom-proxy:9999"}):
            env = self._launch_and_get_env(run_mode=None)
        assert env["DOCKER_HOST"] == "tcp://custom-proxy:9999"

    def test_launch_defaults_docker_host_to_socket_proxy(self):
        """With no DOCKER_HOST in the environment, the agent still points at the
        compose proxy service - never a raw host socket (R6)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCKER_HOST", None)
            env = self._launch_and_get_env(run_mode=None)
        assert env["DOCKER_HOST"] == "tcp://docker-socket-proxy:2375"

    def test_launch_does_not_mount_docker_socket(self):
        """The host Docker socket is never bind-mounted into agent containers,
        and no docker group is added — Docker access is via the proxy only (R6)."""
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

        run_kwargs = cast(MagicMock, mock_client.containers.run).call_args.kwargs
        assert "/var/run/docker.sock" not in run_kwargs["volumes"]
        assert "group_add" not in run_kwargs

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

    def _launch_and_get_env(self, *, run_mode: RunMode | None) -> dict[str, str]:
        """Run launch() (optionally with run_mode) and return the container env."""
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
            if run_mode is None:
                runner.launch("job-123", Path("/app/jobs/job-123"))
            else:
                runner.launch("job-123", Path("/app/jobs/job-123"), run_mode=run_mode)

        return cast(dict[str, str], mock_client.containers.run.call_args.kwargs["environment"])

    def test_launch_sets_run_mode_env_for_report_only(self):
        """REPORT_ONLY launches carry OPENSCIENTIST_RUN_MODE so the entrypoint
        runs only the report-generation phase."""
        env = self._launch_and_get_env(run_mode=RunMode.REPORT_ONLY)
        assert env["OPENSCIENTIST_RUN_MODE"] == "report_only"

    def test_launch_omits_run_mode_env_by_default(self):
        """The default discovery launch keeps a clean env (no run-mode override)."""
        assert "OPENSCIENTIST_RUN_MODE" not in self._launch_and_get_env(run_mode=None)
        assert "OPENSCIENTIST_RUN_MODE" not in self._launch_and_get_env(run_mode=RunMode.DISCOVERY)

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

    def test_get_logs_returns_decoded_tail(self):
        """get_logs returns the decoded tail of the agent container's logs."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.logs.return_value = b"FileNotFoundError: boom\n"
        mock_client.containers.list.return_value = [mock_container]

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            logs = runner.get_logs("job-123")

        assert logs is not None
        assert "FileNotFoundError" in logs
        mock_container.logs.assert_called_once_with(stdout=True, stderr=True, tail=50)

    def test_get_logs_returns_none_when_container_missing(self):
        """get_logs returns None when the agent container cannot be found."""
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            assert runner.get_logs("job-123") is None

    def test_stop_calls_container_stop_with_timeout(self):
        """stop() gracefully stops the labeled agent container with the default timeout."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.list.return_value = [mock_container]

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            runner.stop("job-123")

        mock_container.stop.assert_called_once_with(timeout=10)

    def test_stop_handles_not_found_safely(self):
        """stop() treats a vanished container as a no-op instead of raising."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.stop.side_effect = docker_errors.NotFound("gone")
        mock_client.containers.list.return_value = [mock_container]

        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.logger.warning") as mock_warning,
        ):
            runner = JobContainerRunner()
            runner.stop("job-123")

        mock_warning.assert_not_called()

    def test_stop_noop_when_container_missing(self):
        """stop() is a no-op when no agent container matches the job labels."""
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            runner.stop("job-123")

    def test_cleanup_removes_container_with_force(self):
        """cleanup() force-removes the agent container."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.list.return_value = [mock_container]

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            runner.cleanup("job-123")

        mock_container.remove.assert_called_once_with(force=True)
        mock_container.logs.assert_not_called()

    def test_cleanup_writes_logs_when_log_dir_provided(self, tmp_path: Path) -> None:
        """cleanup() persists container logs before removing the container."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.logs.return_value = b"agent finished\n"
        mock_client.containers.list.return_value = [mock_container]

        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            runner = JobContainerRunner()
            runner.cleanup("job-123", log_dir=tmp_path)

        log_file = tmp_path / "agent-container.log"
        assert log_file.read_text(encoding="utf-8") == "agent finished\n"
        mock_container.logs.assert_called_once_with(stdout=True, stderr=True)
        mock_container.remove.assert_called_once_with(force=True)

    def test_cleanup_removes_container_when_log_retrieval_fails(self, tmp_path: Path) -> None:
        """cleanup() still force-removes the container if log capture fails."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.logs.side_effect = docker_errors.APIError("log read failed")
        mock_client.containers.list.return_value = [mock_container]

        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.logger.warning") as mock_warning,
        ):
            runner = JobContainerRunner()
            runner.cleanup("job-123", log_dir=tmp_path)

        assert not (tmp_path / "agent-container.log").exists()
        mock_container.remove.assert_called_once_with(force=True)
        mock_warning.assert_called_once()


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
        settings.provider.gcp_credentials_host_path = None
        settings.provider.codex_auth_host_path = None
        settings.airgap.enabled = False

        phenix = MagicMock()
        type(phenix).is_available = PropertyMock(return_value=phenix_available)
        phenix.phenix_path = phenix_path
        phenix.phenix_host_path = phenix_host_path
        settings.phenix = phenix

        return settings

    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_phenix_mounted_when_available(self, mock_get_settings, _net):
        """The configured Linux Phenix path is mounted into the agent container."""
        settings = self._mock_settings(
            phenix_available=True,
            phenix_path="/opt/phenix",
            phenix_host_path="/Applications/phenix-1.21.2",
        )
        mock_get_settings.return_value = settings

        runner, mock_client = self._make_runner()
        job_dir = Path("/app/jobs/test-job-id")
        phenix_host = Path("/Applications/phenix-1.21.2")
        expected_host = phenix_host.expanduser().resolve()
        original_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            if path == Path("/var/run/docker.sock"):
                return False
            if path == phenix_host or path == expected_host:
                return True
            return cast(bool, original_exists(path))

        with patch.object(Path, "exists", autospec=True, side_effect=fake_exists):
            runner.launch("test-job-id", job_dir)

        call_kwargs = cast(MagicMock, mock_client.containers.run).call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        phenix_key = Path("/Applications/phenix-1.21.2").expanduser().resolve().as_posix()
        assert phenix_key in volumes
        assert volumes[phenix_key] == {"bind": "/opt/phenix", "mode": "ro"}
        assert "\\" not in phenix_key
        # Match host key via Path equality: resolve()/str() formatting differs by OS.
        phenix_mounts = [spec for host, spec in volumes.items() if Path(host) == expected_host]
        assert len(phenix_mounts) == 1
        assert phenix_mounts[0] == {"bind": "/opt/phenix", "mode": "ro"}
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


class TestGcpCredentialsMount:
    """GCP creds are mounted and advertised only when the operator sets a host
    path. google_application_credentials is the container-internal path baked
    into the web image, so using it as a bind source broke launch for every
    provider (#243)."""

    @staticmethod
    def _settings(*, host_path: str | None) -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        # Always set, exactly as the web image's Dockerfile ENV leaves it.
        provider.google_application_credentials = "/app/gcp-credentials.json"
        provider.gcp_credentials_host_path = host_path
        return SimpleNamespace(
            container=SimpleNamespace(host_project_dir=None, container_app_dir="/app"),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql://db",
                effective_admin_database_url="postgresql://admin-db",
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            airgap=SimpleNamespace(enabled=False),
            secret_key="master-key",
        )

    def test_volumes_omit_creds_without_host_path(self) -> None:
        """No host path means no bind, and never the container-internal path (#243)."""
        volumes = JobContainerRunner._build_container_volumes(
            cast(Settings, self._settings(host_path=None)),
            job_dir_host=Path("/srv/jobs/job-1"),
            job_mount="/agent/jobs/job-1",
        )
        assert "/app/gcp-credentials.json" not in volumes
        assert all(v["bind"] != AGENT_GCP_CREDENTIALS_PATH for v in volumes.values())

    def test_volumes_bind_host_path_read_only(self) -> None:
        """A set host path is bound read-only at the agent mount point."""
        volumes = JobContainerRunner._build_container_volumes(
            cast(Settings, self._settings(host_path="/host/creds.json")),
            job_dir_host=Path("/srv/jobs/job-1"),
            job_mount="/agent/jobs/job-1",
        )
        assert volumes["/host/creds.json"] == {"bind": AGENT_GCP_CREDENTIALS_PATH, "mode": "ro"}

    def _make_runner(self) -> tuple[JobContainerRunner, MagicMock]:
        mock_client = MagicMock()
        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            return JobContainerRunner(), mock_client

    def _launch_settings(self, *, host_path: str | None) -> MagicMock:
        settings = MagicMock()
        settings.container.host_project_dir = None
        settings.container.container_app_dir = "/app"
        settings.secret_key = "test-secret"
        settings.database.effective_database_url = "postgresql+asyncpg://test"
        settings.provider.gcp_credentials_host_path = host_path
        settings.provider.codex_auth_host_path = None
        settings.airgap.enabled = False
        settings.phenix.phenix_host_path = None
        return settings

    @patch("openscientist.job_container.runner.os.stat")
    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_launch_mounts_and_advertises_with_host_path(self, mock_get_settings, _net, mock_stat):
        """With a host path the runner threads the container path so the provider
        emits GOOGLE_APPLICATION_CREDENTIALS, and the creds file is bound."""
        mock_stat.return_value = MagicMock(st_gid=999)
        mock_get_settings.return_value = self._launch_settings(host_path="/host/creds.json")
        runner, mock_client = self._make_runner()

        with patch.object(Path, "exists", return_value=True):
            runner.launch("job-1", Path("/app/jobs/job-1"))

        call = cast(MagicMock, mock_client.containers.run).call_args
        volumes = call.kwargs.get("volumes") or call[1].get("volumes")
        env = call.kwargs.get("environment") or call[1].get("environment")
        assert volumes["/host/creds.json"] == {"bind": AGENT_GCP_CREDENTIALS_PATH, "mode": "ro"}
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == AGENT_GCP_CREDENTIALS_PATH

    @patch("openscientist.job_container.runner.os.stat")
    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_launch_omits_creds_without_host_path(self, mock_get_settings, _net, mock_stat):
        """Without a host path nothing is mounted and no creds env is advertised."""
        mock_stat.return_value = MagicMock(st_gid=999)
        mock_get_settings.return_value = self._launch_settings(host_path=None)
        runner, mock_client = self._make_runner()

        with patch.object(Path, "exists", return_value=True):
            runner.launch("job-1", Path("/app/jobs/job-1"))

        call = cast(MagicMock, mock_client.containers.run).call_args
        volumes = call.kwargs.get("volumes") or call[1].get("volumes")
        env = call.kwargs.get("environment") or call[1].get("environment")
        assert all(v["bind"] != AGENT_GCP_CREDENTIALS_PATH for v in volumes.values())
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env


class TestCodexAuthProvisioning:
    """`CodexAgent.provision_host_prelaunch` copies the codex CLI auth into the
    per-job CODEX_HOME (agent-readable), instead of mounting the host file,
    which the non-root agent could not read across the uid/permission
    boundary. It is the backend's host-side, pre-launch hook."""

    def _settings(self, codex_auth_host_path: str | None) -> MagicMock:
        settings = MagicMock()
        settings.provider.codex_auth_host_path = codex_auth_host_path
        return settings

    def test_copies_auth_into_codex_home_agent_readable(self, tmp_path: Path) -> None:
        from openscientist.agent.codex_agent import CodexAgent

        src = tmp_path / "host-auth.json"
        src.write_text('{"tokens": {}}')
        job_dir = tmp_path / "job"
        job_dir.mkdir()

        CodexAgent.provision_host_prelaunch(self._settings(str(src)), job_dir)

        dest = job_dir / ".codex" / "auth.json"
        assert dest.read_text() == '{"tokens": {}}'
        if os.name == "posix":
            assert (dest.stat().st_mode & 0o777) == 0o644  # agent (uid 1001) can read
            assert (dest.parent.stat().st_mode & 0o777) == 0o777  # agent can write config.toml

    def test_noop_when_unset(self, tmp_path: Path) -> None:
        from openscientist.agent.codex_agent import CodexAgent

        job_dir = tmp_path / "job"
        job_dir.mkdir()
        CodexAgent.provision_host_prelaunch(self._settings(None), job_dir)
        assert not (job_dir / ".codex").exists()

    def test_noop_when_source_missing(self, tmp_path: Path) -> None:
        from openscientist.agent.codex_agent import CodexAgent

        job_dir = tmp_path / "job"
        job_dir.mkdir()
        CodexAgent.provision_host_prelaunch(self._settings(str(tmp_path / "nope.json")), job_dir)
        assert not (job_dir / ".codex" / "auth.json").exists()


class TestJobSecretInjection:
    """The job container receives a per-job derived secret, never the master."""

    @staticmethod
    def _settings(master: str = "master-key") -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        return SimpleNamespace(
            container=SimpleNamespace(host_project_dir=None, container_app_dir="/app"),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql://db",
                effective_admin_database_url="postgresql://admin-db",
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            airgap=SimpleNamespace(enabled=False),
            secret_key=master,
        )

    def test_env_uses_derived_secret_not_master(self) -> None:
        """The injected key is HMAC(master, "job_secret:" + job_id), not the master."""
        settings = self._settings(master="master-key")
        env = JobContainerRunner._build_container_environment(
            cast(Settings, settings), job_id="job-1", job_mount="/agent/jobs/job-1", provider_env={}
        )
        expected = hmac.new(b"master-key", b"job_secret:job-1", hashlib.sha256).hexdigest()
        assert env["OPENSCIENTIST_SECRET_KEY"] == expected
        assert env["OPENSCIENTIST_SECRET_KEY"] != "master-key"

    def test_env_carries_admin_database_url(self) -> None:
        """The agent builds Settings on startup, which refuses to construct without
        ADMIN_DATABASE_URL outside dev mode. Omitting it killed every job at import."""
        settings = self._settings(master="master-key")
        env = JobContainerRunner._build_container_environment(
            cast(Settings, settings), job_id="job-1", job_mount="/agent/jobs/job-1", provider_env={}
        )
        assert env["ADMIN_DATABASE_URL"] == "postgresql://admin-db"

    def test_distinct_job_ids_yield_distinct_secrets(self) -> None:
        """Two jobs get two different injected secrets, and neither is the master."""
        settings = self._settings(master="master-key")
        env_a = JobContainerRunner._build_container_environment(
            cast(Settings, settings),
            job_id="job-a",
            job_mount="/agent/jobs/job-a",
            provider_env={},
        )
        env_b = JobContainerRunner._build_container_environment(
            cast(Settings, settings),
            job_id="job-b",
            job_mount="/agent/jobs/job-b",
            provider_env={},
        )
        secret_a = env_a["OPENSCIENTIST_SECRET_KEY"]
        secret_b = env_b["OPENSCIENTIST_SECRET_KEY"]
        assert secret_a != secret_b
        assert "master-key" not in {secret_a, secret_b}

    def test_derivation_is_deterministic_and_matches_reference(self) -> None:
        """The helper is deterministic and matches a hand-computed reference HMAC."""
        reference = hmac.new(b"master", b"job_secret:job-42", hashlib.sha256).hexdigest()
        assert derive_job_secret("master", "job-42") == reference
        assert derive_job_secret("master", "job-42") == derive_job_secret("master", "job-42")
        assert len(reference) == 64

    def test_derived_value_passes_settings_validation(self) -> None:
        """A Settings built with the derived value validates and derives its own secrets."""
        derived = derive_job_secret("master", "job-validate")
        settings = Settings(OPENSCIENTIST_SECRET_KEY=derived)  # type: ignore[call-arg]
        assert settings.secret_key == derived
        expected_storage = hmac.new(derived.encode(), b"storage_secret", hashlib.sha256).hexdigest()
        assert settings.auth.storage_secret == expected_storage

    def test_env_injects_exec_token_and_broker_url(self) -> None:
        """The container env carries a per-job exec placeholder and the broker URL."""
        settings = self._settings(master="master-key")
        env = JobContainerRunner._build_container_environment(
            cast(Settings, settings), job_id="job-x", job_mount="/agent/jobs/job-x", provider_env={}
        )
        assert env["OPENSCIENTIST_EXEC_TOKEN"] == make_exec_placeholder("master-key", "job-x")
        assert env["OPENSCIENTIST_EXEC_TOKEN"].startswith("job-x.")
        assert env["OPENSCIENTIST_EXEC_BROKER_URL"].endswith(":8082")


class TestAirgapFirewallLaunch:
    """The job container runs behind the nftables egress firewall when air-gapped."""

    @staticmethod
    def _settings(*, airgap: bool, provider_id: str = "ollama") -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        provider.codex_auth_host_path = None
        provider.google_application_credentials = None
        provider.provider_id = provider_id
        provider.ollama_base_url = "http://host.docker.internal:11434/v1"
        provider.aws_region = "us-east-1"
        provider.cloud_ml_region = "us-east5"
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=None,
                container_app_dir="/app",
                agent_network=None,
                agent_memory="8g",
                agent_cpu=2.0,
                agent_platform=None,
                agent_image="openscientist-agent:latest",
            ),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql+asyncpg://u:p@postgres:5432/db",
                effective_admin_database_url="postgresql+asyncpg://a:p@postgres:5432/db",
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            secret_key="secret",
            airgap=SimpleNamespace(enabled=airgap),
        )

    def _launch(
        self, settings: SimpleNamespace, posture: AirgapPosture | None = None
    ) -> dict[str, object]:
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.get_settings", return_value=settings),
            patch(
                "openscientist.job_container.runner.get_provider",
                return_value=_FakeProvider(posture),
            ),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.runner.to_host_path",
                return_value=Path("/app/jobs/job-123"),
            ),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(),
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            runner = JobContainerRunner()
            runner.launch("job-123", Path("/app/jobs/job-123"))
        return cast(dict[str, object], mock_client.containers.run.call_args.kwargs)

    def test_airgap_launch_applies_firewall(self) -> None:
        run_kwargs = self._launch(self._settings(airgap=True))
        assert run_kwargs["cap_add"] == ["NET_ADMIN"]
        assert run_kwargs["user"] == "root"
        assert run_kwargs["entrypoint"] == ["/agent-firewall-entrypoint.sh"]
        environment = cast(dict[str, str], run_kwargs["environment"])
        entries = set(environment["OPENSCIENTIST_FIREWALL_ALLOW"].split(","))
        assert "postgres:5432" in entries
        assert "openscientist:8082" in entries
        # Ollama is a proxied provider now, so the container reaches the proxy,
        # not the model server directly.
        assert "openscientist:8081" in entries
        assert "host.docker.internal:11434" not in entries

    def test_airgap_launch_passes_no_command(self) -> None:
        """The firewall entrypoint execs its arguments and falls back to the agent
        entrypoint only when it gets none, so a command here would silently replace
        the agent with whatever was passed."""
        run_kwargs = self._launch(self._settings(airgap=True))
        assert "command" not in run_kwargs or run_kwargs["command"] is None

    def test_non_airgap_launch_has_no_firewall(self) -> None:
        run_kwargs = self._launch(self._settings(airgap=False))
        assert run_kwargs["cap_add"] is None
        assert run_kwargs["user"] is None
        assert run_kwargs["entrypoint"] is None
        environment = cast(dict[str, str], run_kwargs["environment"])
        assert "OPENSCIENTIST_FIREWALL_ALLOW" not in environment

    def test_airgap_launch_supports_bedrock(self) -> None:
        posture = AirgapPosture(
            AirgapEgress.DIRECT,
            direct_endpoints=(("bedrock-runtime.us-east-1.amazonaws.com", 443),),
        )
        run_kwargs = self._launch(self._settings(airgap=True, provider_id="bedrock"), posture)
        environment = cast(dict[str, str], run_kwargs["environment"])
        entries = set(environment["OPENSCIENTIST_FIREWALL_ALLOW"].split(","))
        assert "bedrock-runtime.us-east-1.amazonaws.com:443" in entries
        assert "openscientist:8081" not in entries


class TestChatTurnLaunch:
    """In-page chat runs one turn in an ephemeral, hardened container."""

    @staticmethod
    def _settings() -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        provider.codex_auth_host_path = None
        provider.google_application_credentials = None
        provider.provider_id = "anthropic"
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=None,
                container_app_dir="/app",
                agent_network=None,
                agent_memory="8g",
                agent_cpu=2.0,
                agent_platform=None,
                agent_image="openscientist-agent:latest",
            ),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql+asyncpg://u:p@postgres:5432/db",
                effective_admin_database_url="postgresql+asyncpg://a:p@postgres:5432/db",
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            secret_key="master-key",
            airgap=SimpleNamespace(enabled=False),
        )

    def _run_chat(self, settings: SimpleNamespace, *, exit_code: int = 0) -> Any:
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_container.wait.return_value = {"StatusCode": exit_code}
        mock_container.logs.return_value = b"container log"
        mock_client.containers.run.return_value = mock_container
        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch("openscientist.job_container.runner.get_settings", return_value=settings),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.runner.to_host_path",
                return_value=Path("/app/jobs/job-123"),
            ),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(),
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            JobContainerRunner().run_chat_turn("job-123", Path("/app/jobs/job-123"))
        return mock_client, mock_container

    def test_chat_turn_launches_hardened_container(self) -> None:
        mock_client, mock_container = self._run_chat(self._settings())
        run_kwargs = cast(dict[str, object], mock_client.containers.run.call_args.kwargs)
        labels = cast(dict[str, str], run_kwargs["labels"])
        assert labels["openscientist.type"] == "chat"
        assert cast(str, run_kwargs["name"]).startswith("openscientist-chat-")
        env = cast(dict[str, str], run_kwargs["environment"])
        # The chat container carries the per-job derived secret, never the master.
        assert env["OPENSCIENTIST_SECRET_KEY"] != "master-key"
        assert env["OPENSCIENTIST_RUN_MODE"] == "chat"
        mock_container.wait.assert_called_once()
        mock_container.remove.assert_called_once_with(force=True)

    def test_chat_turn_raises_on_nonzero_exit(self) -> None:
        with pytest.raises(RuntimeError, match="exited with code 1"):
            self._run_chat(self._settings(), exit_code=1)

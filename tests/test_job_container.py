"""Tests for openscientist.job_container module."""

import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from docker import errors as docker_errors
from openscientist.job_container.runner import AGENT_APP_DIR, JobContainerRunner
from openscientist.job_container.secrets import derive_job_secret, make_exec_placeholder
from openscientist.settings import Settings


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

    def _launch_and_get_env(self, *, run_mode: str | None) -> dict[str, str]:
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
        """report_only launches carry OPENSCIENTIST_RUN_MODE so the entrypoint
        runs only the report-generation phase."""
        env = self._launch_and_get_env(run_mode="report_only")
        assert env["OPENSCIENTIST_RUN_MODE"] == "report_only"

    def test_launch_omits_run_mode_env_by_default(self):
        """The default discovery launch keeps a clean env (no run-mode override)."""
        assert "OPENSCIENTIST_RUN_MODE" not in self._launch_and_get_env(run_mode=None)
        assert "OPENSCIENTIST_RUN_MODE" not in self._launch_and_get_env(run_mode="discovery")

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
        settings.provider.codex_auth_host_path = None

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
            database=SimpleNamespace(effective_database_url="postgresql://db"),
            phenix=SimpleNamespace(phenix_host_path=None),
            secret_key=master,
        )

    def test_env_uses_derived_secret_not_master(self) -> None:
        """The injected key is HMAC(master, "job_secret:" + job_id), not the master."""
        settings = self._settings(master="master-key")
        env = JobContainerRunner._build_container_environment(
            cast(Settings, settings), job_id="job-1", job_mount="/agent/jobs/job-1"
        )
        expected = hmac.new(b"master-key", b"job_secret:job-1", hashlib.sha256).hexdigest()
        assert env["OPENSCIENTIST_SECRET_KEY"] == expected
        assert env["OPENSCIENTIST_SECRET_KEY"] != "master-key"

    def test_distinct_job_ids_yield_distinct_secrets(self) -> None:
        """Two jobs get two different injected secrets, and neither is the master."""
        settings = self._settings(master="master-key")
        env_a = JobContainerRunner._build_container_environment(
            cast(Settings, settings), job_id="job-a", job_mount="/agent/jobs/job-a"
        )
        env_b = JobContainerRunner._build_container_environment(
            cast(Settings, settings), job_id="job-b", job_mount="/agent/jobs/job-b"
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
            cast(Settings, settings), job_id="job-x", job_mount="/agent/jobs/job-x"
        )
        assert env["OPENSCIENTIST_EXEC_TOKEN"] == make_exec_placeholder("master-key", "job-x")
        assert env["OPENSCIENTIST_EXEC_TOKEN"].startswith("job-x.")
        assert env["OPENSCIENTIST_EXEC_BROKER_URL"].endswith(":8082")

"""Tests for openscientist.job_container module."""

import asyncio
import hashlib
import hmac
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from docker import errors as docker_errors
from openscientist.exceptions import MissingImageError
from openscientist.exec_broker_client import EXEC_TOKEN_ENV
from openscientist.job_chat import CHAT_TURN_ENV
from openscientist.job_container.db_login import AGENT_LOGIN_LABEL, AgentLogin
from openscientist.job_container.runner import (
    _STOP_GRACE_SECONDS,
    AGENT_APP_DIR,
    JobContainerRunner,
)
from openscientist.job_container.secrets import (
    broker_accepts,
    derive_job_secret,
    proxy_accepts,
    revoke_run_credentials,
)
from openscientist.providers.base import AirgapEgress, AirgapPosture
from openscientist.settings import Settings, clear_settings_cache
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.helpers import assert_names_a_build_command


@pytest.fixture(autouse=True)
def _no_airgap_floor(monkeypatch: pytest.MonkeyPatch):
    """The environment without the airgap floor, so the row alone decides."""
    monkeypatch.setenv("OPENSCIENTIST_AIRGAPPED", "false")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest_asyncio.fixture(autouse=True)
async def admin_settings_row(test_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """The row each launch reads the limits, lifetime, and airgap flag from."""
    await set_admin_settings_row(test_engine)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


class _FakeProvider:
    #: The LLM placeholder of the latest launch.
    last_placeholder = ""

    def proxied_container_env(
        self,
        *,
        proxy_base_url: str,
        placeholder: str,
    ) -> dict[str, str]:
        _FakeProvider.last_placeholder = placeholder
        return {"EXTRA_ENV": "1"}

    def prelaunch_model_context_env(self) -> dict[str, str]:
        return {}

    def prelaunch_routing_error(self) -> str | None:
        return None

    def airgap_egress(self) -> AirgapPosture:
        return AirgapPosture(AirgapEgress.PROXY)


@pytest.fixture(autouse=True)
def _fake_provider():
    with patch("openscientist.job_container.runner.get_provider", return_value=_FakeProvider()):
        yield


_LOGIN = AgentLogin(
    role="agent_" + "0" * 32,
    database_url="postgresql+asyncpg://agent_x:login-pw@postgres:5432/db",
)


@pytest.fixture(autouse=True)
def agent_login():
    """Stand in for the database: every launch gets ``_LOGIN``."""
    with (
        patch("openscientist.job_container.runner.open_agent_login", return_value=_LOGIN) as opened,
        patch("openscientist.job_container.runner.close_agent_login") as closed,
    ):
        yield SimpleNamespace(opened=opened, closed=closed)


class TestContainerDatabaseLogin:
    """Each agent container reaches the database through its own login, which
    is given back when that container ends."""

    @staticmethod
    def _settings() -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        provider.codex_auth_host_path = None
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
                agent_timeout=14400,
            ),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql+asyncpg://web:web-pw@postgres:5432/db"
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            secret_key="master-key",
            airgap=SimpleNamespace(enabled=False),
        )

    def _runner(self, mock_client: MagicMock) -> JobContainerRunner:
        with patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client):
            return JobContainerRunner()

    def _launch_patches(self):
        return (
            patch("openscientist.job_container.runner.get_settings", return_value=self._settings()),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.runner.to_host_path",
                return_value=Path("/app/jobs/job-123"),
            ),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(RUN_HOME=None),
            ),
        )

    def test_the_container_gets_its_own_login_and_never_the_web_one(self, agent_login) -> None:
        mock_client = MagicMock()
        a, b, c, d = self._launch_patches()
        with a, b, c, d:
            self._runner(mock_client).launch("job-123", Path("/app/jobs/job-123"))
        kwargs = mock_client.containers.run.call_args.kwargs
        assert kwargs["environment"]["DATABASE_URL"] == _LOGIN.database_url
        assert not any("web-pw" in value for value in kwargs["environment"].values())
        assert kwargs["labels"][AGENT_LOGIN_LABEL] == _LOGIN.role
        agent_login.opened.assert_called_once_with("job-123", lifetime_seconds=14400)

    def test_the_container_gets_live_credentials_of_its_own_run(self, agent_login) -> None:
        mock_client = MagicMock()
        a, b, c, d = self._launch_patches()
        with a, b, c, d:
            self._runner(mock_client).launch("job-123", Path("/app/jobs/job-123"))
        try:
            env = mock_client.containers.run.call_args.kwargs["environment"]
            assert broker_accepts("job-123", env[EXEC_TOKEN_ENV])
            assert proxy_accepts(_FakeProvider.last_placeholder)
        finally:
            revoke_run_credentials(_LOGIN.role)

    def test_no_container_starts_without_its_login(self, agent_login) -> None:
        agent_login.opened.side_effect = RuntimeError("cannot create role")
        mock_client = MagicMock()
        a, b, c, d = self._launch_patches()
        with a, b, c, d, pytest.raises(RuntimeError, match="cannot create role"):
            self._runner(mock_client).launch("job-123", Path("/app/jobs/job-123"))
        mock_client.containers.run.assert_not_called()

    def test_unroutable_provider_traffic_refuses_before_the_container_starts(
        self, agent_login
    ) -> None:
        """Unroutable proxied traffic refuses at launch, before any container starts."""
        unroutable = MagicMock()
        unroutable.prelaunch_routing_error.return_value = (
            "the omp harness cannot route the ChatGPT login"
        )
        mock_client = MagicMock()
        a, b, c, d = self._launch_patches()
        with (
            a,
            b,
            c,
            d,
            patch(
                "openscientist.job_container.runner.get_provider",
                return_value=unroutable,
            ),
            pytest.raises(RuntimeError, match="cannot route the ChatGPT login"),
        ):
            self._runner(mock_client).launch("job-123", Path("/app/jobs/job-123"))
        mock_client.containers.run.assert_not_called()

    def test_a_container_that_fails_to_start_gives_its_login_back(self, agent_login) -> None:
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = docker_errors.APIError("no such image")
        a, b, c, d = self._launch_patches()
        with a, b, c, d, pytest.raises(docker_errors.APIError):
            self._runner(mock_client).launch("job-123", Path("/app/jobs/job-123"))
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_a_missing_agent_image_fails_with_how_to_build_it(self, agent_login) -> None:
        """The job's error names the image and a real target that builds it,
        and the run still gives its login back."""
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = docker_errors.ImageNotFound(
            "No such image: openscientist-agent:latest"
        )
        a, b, c, d = self._launch_patches()
        with a, b, c, d, pytest.raises(MissingImageError) as raised:
            self._runner(mock_client).launch("job-123", Path("/app/jobs/job-123"))
        assert "openscientist-agent:latest" in str(raised.value)
        assert_names_a_build_command(str(raised.value))
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_cleanup_gives_back_the_login_of_the_removed_container(self, agent_login) -> None:
        """Startup cleanup uses a fresh runner, so the login name comes from the
        container's label and not from runner state."""
        container = MagicMock()
        container.labels = {AGENT_LOGIN_LABEL: _LOGIN.role}
        mock_client = MagicMock()
        mock_client.containers.list.return_value = [container]
        self._runner(mock_client).cleanup("job-123")
        container.remove.assert_called_once_with(force=True)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_a_failed_chat_turn_gives_its_login_back(self, agent_login) -> None:
        container = MagicMock()
        container.labels = {AGENT_LOGIN_LABEL: _LOGIN.role}
        container.wait.return_value = {"StatusCode": 1}
        container.logs.return_value = b"boom"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = container
        a, b, c, d = self._launch_patches()
        with a, b, c, d, pytest.raises(RuntimeError, match="exited with code 1"):
            self._runner(mock_client).run_chat_turn("job-123", Path("/app/jobs/job-123"), "t1")
        agent_login.opened.assert_called_once_with("job-123", lifetime_seconds=300)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_live_logins_are_the_labels_of_every_listed_container(self) -> None:
        """Exited containers count too, since their teardown has yet to run."""
        running, exited = MagicMock(), MagicMock()
        running.labels = {AGENT_LOGIN_LABEL: "agent_" + "1" * 32}
        exited.labels = {AGENT_LOGIN_LABEL: "agent_" + "2" * 32}
        mock_client = MagicMock()
        mock_client.containers.list.return_value = [running, exited]
        assert self._runner(mock_client).live_login_roles() == {
            "agent_" + "1" * 32,
            "agent_" + "2" * 32,
        }
        assert mock_client.containers.list.call_args.kwargs["all"] is True

    def test_live_logins_are_unknown_when_docker_fails(self) -> None:
        mock_client = MagicMock()
        mock_client.containers.list.side_effect = docker_errors.DockerException("down")
        assert self._runner(mock_client).live_login_roles() is None


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
                agent_timeout=14400,
            ),
            provider=provider,
            database=SimpleNamespace(effective_database_url="postgresql://db"),
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

        with patch.object(Path, "exists", return_value=True):
            runner.launch("test-job-id", job_dir)

        call_kwargs = cast(MagicMock, mock_client.containers.run).call_args
        volumes = call_kwargs.kwargs.get("volumes") or call_kwargs[1].get("volumes")
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")

        assert "/Applications/phenix-1.21.2" in volumes
        assert volumes["/Applications/phenix-1.21.2"] == {"bind": "/opt/phenix", "mode": "ro"}
        assert env["PHENIX_PATH"] == "/opt/phenix"

    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_phenix_not_mounted_without_host_path(self, mock_get_settings, _net):
        """Phenix is not mounted when phenix_host_path is unset."""
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

    @patch("openscientist.job_container.runner.resolve_docker_network", return_value="bridge")
    @patch("openscientist.job_container.runner.get_settings")
    def test_phenix_not_mounted_when_unavailable(self, mock_get_settings, _net):
        """Phenix mounts are omitted when the feature is unavailable."""
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
            airgap=SimpleNamespace(enabled=False),
            secret_key=master,
        )

    def test_env_uses_derived_secret_not_master(self) -> None:
        """The injected key is HMAC(master, "job_secret:" + job_id), not the master."""
        settings = self._settings(master="master-key")
        env = JobContainerRunner._build_container_environment(
            cast(Settings, settings),
            job_id="job-1",
            job_mount="/agent/jobs/job-1",
            provider_env={},
            database_url="postgresql://agent",
            exec_token="job-1.exec",
            airgapped=False,
        )
        expected = hmac.new(b"master-key", b"job_secret:job-1", hashlib.sha256).hexdigest()
        assert env["OPENSCIENTIST_SECRET_KEY"] == expected
        assert env["OPENSCIENTIST_SECRET_KEY"] != "master-key"

    def test_distinct_job_ids_yield_distinct_secrets(self) -> None:
        """Two jobs get two different injected secrets, and neither is the master."""
        settings = self._settings(master="master-key")
        env_a = JobContainerRunner._build_container_environment(
            cast(Settings, settings),
            job_id="job-a",
            job_mount="/agent/jobs/job-a",
            provider_env={},
            database_url="postgresql://agent",
            exec_token="job-a.exec",
            airgapped=False,
        )
        env_b = JobContainerRunner._build_container_environment(
            cast(Settings, settings),
            job_id="job-b",
            job_mount="/agent/jobs/job-b",
            provider_env={},
            database_url="postgresql://agent",
            exec_token="job-b.exec",
            airgapped=False,
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


class TestAirgapFirewallLaunch:
    """The job container runs behind the nftables egress firewall when air-gapped."""

    @staticmethod
    def _settings(*, provider_id: str = "ollama") -> SimpleNamespace:
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
                agent_platform=None,
                agent_image="openscientist-agent:latest",
            ),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql+asyncpg://u:p@postgres:5432/db"
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            secret_key="secret",
        )

    async def _launch(
        self,
        test_engine: AsyncEngine,
        airgapped: bool,
        provider_id: str = "ollama",
    ) -> dict[str, object]:
        """Launch with the row's airgap flag, and return the container run kwargs."""
        await set_admin_settings_row(test_engine, airgapped=airgapped)
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_client.containers.run.return_value = mock_container
        with (
            patch("openscientist.job_container.runner.docker.from_env", return_value=mock_client),
            patch(
                "openscientist.job_container.runner.get_settings",
                return_value=self._settings(provider_id=provider_id),
            ),
            patch(
                "openscientist.job_container.runner.get_provider",
                return_value=_FakeProvider(),
            ),
            patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
            patch(
                "openscientist.job_container.runner.to_host_path",
                return_value=Path("/app/jobs/job-123"),
            ),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(RUN_HOME=None),
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            runner = JobContainerRunner()
            await asyncio.to_thread(runner.launch, "job-123", Path("/app/jobs/job-123"))
        return cast(dict[str, object], mock_client.containers.run.call_args.kwargs)

    @pytest.mark.asyncio
    async def test_airgap_launch_applies_firewall(self, test_engine: AsyncEngine) -> None:
        run_kwargs = await self._launch(test_engine, airgapped=True)
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

    @pytest.mark.asyncio
    async def test_airgap_launch_passes_no_command(self, test_engine: AsyncEngine) -> None:
        """The firewall entrypoint execs its arguments and falls back to the agent
        entrypoint only when it gets none, so a command here would silently replace
        the agent with whatever was passed."""
        run_kwargs = await self._launch(test_engine, airgapped=True)
        assert "command" not in run_kwargs or run_kwargs["command"] is None

    @pytest.mark.asyncio
    async def test_non_airgap_launch_has_no_firewall(self, test_engine: AsyncEngine) -> None:
        run_kwargs = await self._launch(test_engine, airgapped=False)
        assert run_kwargs["cap_add"] is None
        assert run_kwargs["user"] is None
        assert run_kwargs["entrypoint"] is None
        environment = cast(dict[str, str], run_kwargs["environment"])
        assert "OPENSCIENTIST_FIREWALL_ALLOW" not in environment


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
                agent_timeout=14400,
            ),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql+asyncpg://u:p@postgres:5432/db"
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            secret_key="master-key",
            airgap=SimpleNamespace(enabled=False),
        )

    def _run_chat(
        self,
        settings: SimpleNamespace,
        *,
        exit_code: int = 0,
        stop_error: Exception | None = None,
    ):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.short_id = "abc123"
        mock_container.wait.return_value = {"StatusCode": exit_code}
        mock_container.logs.return_value = b"container log"
        mock_container.labels = {AGENT_LOGIN_LABEL: _LOGIN.role}
        if stop_error is not None:
            mock_container.stop.side_effect = stop_error
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
                return_value=MagicMock(RUN_HOME=None),
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            JobContainerRunner().run_chat_turn("job-123", Path("/app/jobs/job-123"), "t1")
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
        assert env[CHAT_TURN_ENV] == "t1"
        mock_container.wait.assert_called_once()
        # Stopped with the grace a turn needs to write its cost, then removed.
        calls = [c for c in mock_container.method_calls if c[0] in ("stop", "remove")]
        assert calls == [call.stop(timeout=_STOP_GRACE_SECONDS), call.remove(force=True)]

    def test_chat_turn_raises_on_nonzero_exit(self) -> None:
        with pytest.raises(RuntimeError, match="exited with code 1"):
            self._run_chat(self._settings(), exit_code=1)

    def test_chat_turn_removes_container_and_drops_login_when_stop_fails(
        self,
        agent_login: SimpleNamespace,
    ) -> None:
        _, mock_container = self._run_chat(
            self._settings(),
            stop_error=docker_errors.APIError("stop failed"),
        )
        # A failed stop must not lose the cleanup either.
        mock_container.remove.assert_called_once_with(force=True)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_chat_turn_tolerates_stop_when_container_is_gone(
        self,
        agent_login: SimpleNamespace,
    ) -> None:
        _, mock_container = self._run_chat(
            self._settings(),
            stop_error=docker_errors.NotFound("no such container"),
        )
        # A container already gone is swallowed silently, cleanup still runs.
        mock_container.remove.assert_called_once_with(force=True)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

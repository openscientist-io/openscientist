"""Tests for openscientist.job_container module."""

import asyncio
import hashlib
import hmac
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, call, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.exceptions import MissingImageError
from openscientist.exec_broker_client import EXEC_TOKEN_ENV
from openscientist.job_container import runner as runner_module
from openscientist.job_container.db_login import AGENT_LOGIN_LABEL, AgentLogin
from openscientist.job_container.launcher_client import (
    LauncherClient,
    LauncherDockerError,
    LauncherImageMissingError,
    LauncherNotFoundError,
    LauncherUnavailableError,
)
from openscientist.job_container.run_home import create_run_home
from openscientist.job_container.runner import (
    _STOP_GRACE_SECONDS,
    AGENT_APP_DIR,
    JobContainerRunner,
)
from openscientist.job_container.secrets import (
    broker_accepts,
    derive_job_secret,
    issue_run_credentials,
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


def _runner_with_client() -> tuple[JobContainerRunner, MagicMock]:
    """A runner whose launcher client is a mock; the created handle is fixed."""
    runner = JobContainerRunner()
    client = MagicMock(spec=LauncherClient)
    client.create_agent.return_value = {
        "id": "c" * 64,
        "short_id": "abc123",
        "name": "openscientist-agent-job1",
        "status": "running",
    }
    runner._client = client
    return runner, client


def _request(client: MagicMock) -> dict[str, object]:
    return cast(dict[str, object], client.create_agent.call_args.args[0])


def _listed_entry(labels: dict[str, str], container_id: str = "d" * 64) -> dict[str, object]:
    return {
        "id": container_id,
        "name": "openscientist-agent-job1",
        "status": "running",
        "created": "2026-11-03T00:00:00Z",
        "labels": labels,
    }


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
                agent_image="openscientist-agent:latest",
                agent_pids_limit=512,
            ),
            provider=provider,
            database=SimpleNamespace(
                effective_database_url="postgresql+asyncpg://web:web-pw@postgres:5432/db"
            ),
            phenix=SimpleNamespace(phenix_host_path=None),
            secret_key="master-key",
            airgap=SimpleNamespace(enabled=False),
        )

    def _launch_patches(self):
        return (
            patch("openscientist.job_container.runner.get_settings", return_value=self._settings()),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(RUN_HOME=None),
            ),
        )

    def test_the_container_gets_its_own_login_and_never_the_web_one(self, agent_login) -> None:
        runner, client = _runner_with_client()
        a, b = self._launch_patches()
        with a, b:
            runner.launch("job-123", Path("/app/jobs/job-123"))
        request = _request(client)
        env = cast(dict[str, str], request["env"])
        assert env["DATABASE_URL"] == _LOGIN.database_url
        assert not any("web-pw" in value for value in env.values())
        assert request["db_login"] == _LOGIN.role
        agent_login.opened.assert_called_once_with("job-123", lifetime_seconds=14400)

    def test_the_container_gets_live_credentials_of_its_own_run(self, agent_login) -> None:
        runner, client = _runner_with_client()
        a, b = self._launch_patches()
        with a, b:
            runner.launch("job-123", Path("/app/jobs/job-123"))
        try:
            env = cast(dict[str, str], _request(client)["env"])
            assert broker_accepts("job-123", env[EXEC_TOKEN_ENV])
            assert proxy_accepts(_FakeProvider.last_placeholder)
        finally:
            revoke_run_credentials(_LOGIN.role)

    def test_no_container_starts_without_its_login(self, agent_login) -> None:
        agent_login.opened.side_effect = RuntimeError("cannot create role")
        runner, client = _runner_with_client()
        a, b = self._launch_patches()
        with a, b, pytest.raises(RuntimeError, match="cannot create role"):
            runner.launch("job-123", Path("/app/jobs/job-123"))
        client.create_agent.assert_not_called()

    def test_unroutable_provider_traffic_refuses_before_the_container_starts(
        self, agent_login
    ) -> None:
        """Unroutable proxied traffic refuses at launch, before any container starts."""
        unroutable = MagicMock()
        unroutable.prelaunch_routing_error.return_value = (
            "the omp harness cannot route the ChatGPT login"
        )
        runner, client = _runner_with_client()
        a, b = self._launch_patches()
        with (
            a,
            b,
            patch(
                "openscientist.job_container.runner.get_provider",
                return_value=unroutable,
            ),
            pytest.raises(RuntimeError, match="cannot route the ChatGPT login"),
        ):
            runner.launch("job-123", Path("/app/jobs/job-123"))
        client.create_agent.assert_not_called()

    def test_a_container_that_fails_to_start_gives_its_login_back(self, agent_login) -> None:
        runner, client = _runner_with_client()
        client.create_agent.side_effect = LauncherDockerError(
            "docker_error", "the daemon refused the run"
        )
        a, b = self._launch_patches()
        with a, b, pytest.raises(LauncherDockerError):
            runner.launch("job-123", Path("/app/jobs/job-123"))
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_a_missing_agent_image_fails_with_how_to_build_it(self, agent_login) -> None:
        """The job's error names the image and a real target that builds it,
        and the run still gives its login back."""
        runner, client = _runner_with_client()
        client.create_agent.side_effect = LauncherImageMissingError(
            "image_missing", "the agent image is not available"
        )
        a, b = self._launch_patches()
        with a, b, pytest.raises(MissingImageError) as raised:
            runner.launch("job-123", Path("/app/jobs/job-123"))
        assert "openscientist-agent:latest" in str(raised.value)
        assert_names_a_build_command(str(raised.value))
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_cleanup_gives_back_the_login_of_the_removed_container(self, agent_login) -> None:
        """Startup cleanup uses a fresh runner, so the login name comes from the
        container's label and not from runner state."""
        runner, client = _runner_with_client()
        client.list_containers.return_value = [_listed_entry({AGENT_LOGIN_LABEL: _LOGIN.role})]
        runner.cleanup("job-123")
        client.remove.assert_called_once_with("d" * 64)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_cleanup_takes_the_container_down_even_when_stop_fails(
        self, tmp_path: Path, agent_login
    ) -> None:
        """A stop error must not strand the container: the agent got its grace
        to finish in-flight work, and teardown still saves the log, removes the
        container and drops its database login."""
        runner, client = _runner_with_client()
        client.list_containers.return_value = [_listed_entry({AGENT_LOGIN_LABEL: _LOGIN.role})]
        client.stop.side_effect = LauncherDockerError("docker", "stop failed")
        client.logs.return_value = "log saved"
        runner.cleanup("job-123", tmp_path)
        client.stop.assert_called_once_with("d" * 64, timeout_seconds=_STOP_GRACE_SECONDS)
        assert (tmp_path / "agent-container.log").read_text() == "log saved"
        client.remove.assert_called_once_with("d" * 64)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_cleanup_removes_the_run_home_named_in_the_labels(self, tmp_path: Path) -> None:
        """The home the launcher reports on the label comes back to the web's side
        (host -> container path) before removal."""
        home = create_run_home(tmp_path / "jobs", _LOGIN.role)
        runner, client = _runner_with_client()
        client.list_containers.return_value = [
            _listed_entry(
                {
                    AGENT_LOGIN_LABEL: _LOGIN.role,
                    "openscientist.run_home": str(home),
                }
            )
        ]
        with patch("openscientist.job_container.runner.get_settings") as mock_settings:
            # No host project dir: the host path is the web's path unchanged.
            mock_settings.return_value = self._settings()
            runner.cleanup("job-123")
        assert not home.exists()
        client.remove.assert_called_once_with("d" * 64)

    def test_a_failed_chat_turn_gives_its_login_back(self, agent_login) -> None:
        runner, client = _runner_with_client()
        client.wait.return_value = {"exit_code": 1, "timeout": False}
        client.logs.return_value = "boom"
        a, b = self._launch_patches()
        with a, b, pytest.raises(RuntimeError, match="exited with code 1"):
            runner.run_chat_turn("job-123", Path("/app/jobs/job-123"), "t1")
        agent_login.opened.assert_called_once_with("job-123", lifetime_seconds=300)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_live_logins_are_the_labels_of_every_listed_container(self) -> None:
        """Exited containers count too, since their teardown has yet to run."""
        runner, client = _runner_with_client()
        client.list_containers.return_value = [
            _listed_entry({AGENT_LOGIN_LABEL: "agent_" + "1" * 32}),
            _listed_entry({AGENT_LOGIN_LABEL: "agent_" + "2" * 32}),
        ]
        assert runner.live_login_roles() == {
            "agent_" + "1" * 32,
            "agent_" + "2" * 32,
        }
        client.list_containers.assert_called_once_with([AGENT_LOGIN_LABEL])

    def test_live_logins_are_unknown_when_the_launcher_fails(self) -> None:
        runner, client = _runner_with_client()
        client.list_containers.side_effect = LauncherUnavailableError(
            "unavailable", "the launcher could not be reached"
        )
        assert runner.live_login_roles() is None


_UNREACHABLE = LauncherUnavailableError("unavailable", "the launcher did not answer within 30s")


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "_LOOKUP_RETRY_DELAYS", (0.0, 0.0), raising=False)


class TestTeardownWhenTheLauncherFails:
    """A failed launcher call is never taken for a job without a container."""

    @pytest.fixture
    def held_runs(self):
        """A discovery and a chat run of job-123, both still accepted."""
        roles = {"discovery": "agent_" + "1" * 32, "chat": "agent_" + "2" * 32}
        issue_run_credentials("job-123", roles["discovery"], "discovery")
        issue_run_credentials("job-123", roles["chat"], "chat")
        try:
            yield roles
        finally:
            for role in roles.values():
                revoke_run_credentials(role)

    def test_cleanup_retries_a_failed_lookup_and_takes_the_container_down(
        self, agent_login, no_retry_wait
    ) -> None:
        runner, client = _runner_with_client()
        client.list_containers.side_effect = [
            _UNREACHABLE,
            [_listed_entry({AGENT_LOGIN_LABEL: _LOGIN.role})],
        ]
        runner.cleanup("job-123")
        client.stop.assert_called_once_with("d" * 64, timeout_seconds=_STOP_GRACE_SECONDS)
        client.remove.assert_called_once_with("d" * 64)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_cleanup_drops_the_jobs_agent_logins_when_the_launcher_stays_down(
        self, agent_login, no_retry_wait, held_runs, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The chat run of the same job keeps its login, since its own
        teardown ends it."""
        runner, client = _runner_with_client()
        client.list_containers.side_effect = _UNREACHABLE
        with caplog.at_level(logging.ERROR, logger="openscientist.job_container.runner"):
            runner.cleanup("job-123")
        client.remove.assert_not_called()
        agent_login.closed.assert_called_once_with(held_runs["discovery"])
        assert [r.levelno for r in caplog.records if "job-123" in r.getMessage()] == [logging.ERROR]

    def test_cancelling_reports_a_failed_lookup(self) -> None:
        runner, client = _runner_with_client()
        client.list_containers.side_effect = _UNREACHABLE
        with pytest.raises(LauncherUnavailableError):
            runner.stop("job-123")

    def test_the_reaper_takes_down_containers_whose_run_is_no_longer_held(
        self, agent_login, held_runs
    ) -> None:
        runner, client = _runner_with_client()
        unheld = "agent_" + "3" * 32
        client.list_containers.return_value = [
            _listed_entry({AGENT_LOGIN_LABEL: held_runs["discovery"]}, container_id="h" * 64),
            _listed_entry({AGENT_LOGIN_LABEL: unheld}, container_id="u" * 64),
        ]
        runner.remove_unheld_containers()
        client.stop.assert_called_once_with("u" * 64, timeout_seconds=_STOP_GRACE_SECONDS)
        client.remove.assert_called_once_with("u" * 64)
        agent_login.closed.assert_called_once_with(unheld)

    def test_a_relaunch_goes_ahead_when_the_stale_check_fails(self, agent_login) -> None:
        """The create call answers for the launcher, so a failed lookup of a
        stale container does not stop the launch."""
        runner, client = _runner_with_client()
        client.list_containers.side_effect = _UNREACHABLE
        a, b = TestContainerDatabaseLogin()._launch_patches()
        with a, b:
            handle = runner.launch("job-123", Path("/app/jobs/job-123"))
        assert handle["short_id"] == "abc123"
        client.remove.assert_not_called()

    def test_the_exit_code_reads_as_running_when_the_lookup_fails(self) -> None:
        runner, client = _runner_with_client()
        client.list_containers.side_effect = _UNREACHABLE
        assert runner.get_exit_code("job-123") is None
        client.inspect.assert_not_called()

    def test_the_reaper_leaves_everything_when_the_launcher_cannot_list(self, agent_login) -> None:
        runner, client = _runner_with_client()
        client.list_containers.side_effect = _UNREACHABLE
        runner.remove_unheld_containers()
        client.stop.assert_not_called()
        agent_login.closed.assert_not_called()

    def test_a_teardown_ends_the_run_when_logs_and_removal_fail(
        self, agent_login, tmp_path: Path
    ) -> None:
        """The run's login goes even when the logs cannot be saved and the
        container cannot be removed, so the reaper only has the container left."""
        runner, client = _runner_with_client()
        client.list_containers.return_value = [_listed_entry({AGENT_LOGIN_LABEL: _LOGIN.role})]
        client.logs.side_effect = LauncherDockerError("docker", "logs failed")
        client.remove.side_effect = LauncherDockerError("docker", "remove failed")
        runner.cleanup("job-123", tmp_path)
        assert not (tmp_path / "agent-container.log").exists()
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_a_teardown_ends_the_run_of_a_container_already_gone(self, agent_login) -> None:
        runner, client = _runner_with_client()
        client.list_containers.return_value = [_listed_entry({AGENT_LOGIN_LABEL: _LOGIN.role})]
        client.remove.side_effect = LauncherNotFoundError("not_found", "container not found")
        runner.cleanup("job-123")
        agent_login.closed.assert_called_once_with(_LOGIN.role)


class TestJobContainerRunner:
    """Tests for JobContainerRunner."""

    @staticmethod
    def _make_settings(*, host_project_dir: str | None) -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {"EXTRA_ENV": "1"}
        provider.codex_auth_host_path = None
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=host_project_dir,
                container_app_dir="/app",
                agent_pids_limit=256,
            ),
            provider=provider,
            database=SimpleNamespace(effective_database_url="postgresql://db"),
            phenix=SimpleNamespace(phenix_host_path=None),
            airgap=SimpleNamespace(enabled=False),
            secret_key="secret",
        )

    def _launch_patches(self, settings: SimpleNamespace):
        return (
            patch("openscientist.job_container.runner.get_settings", return_value=settings),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(RUN_HOME=None),
            ),
        )

    def test_client_is_built_lazily_and_reused(self, tmp_path: Path) -> None:
        """Construction stays cheap; the socket is only reached on a request."""
        import os

        old = os.environ.get("OPENSCIENTIST_LAUNCHER_SOCKET_PATH")
        os.environ["OPENSCIENTIST_LAUNCHER_SOCKET_PATH"] = str(tmp_path / "launcher.sock")
        clear_settings_cache()
        try:
            runner = JobContainerRunner()
            assert isinstance(runner.client, LauncherClient)
            assert runner.client is runner.client
        finally:
            if old is None:
                del os.environ["OPENSCIENTIST_LAUNCHER_SOCKET_PATH"]
            else:
                os.environ["OPENSCIENTIST_LAUNCHER_SOCKET_PATH"] = old
            clear_settings_cache()

    def test_launch_passes_host_path_mapping_to_agent_container(self):
        """Launch passes the host project mapping the launcher's env allow-list
        admits, and the job dir the agent sees inside its container."""
        runner, client = _runner_with_client()
        settings = self._make_settings(host_project_dir="/host/project")
        a, b = self._launch_patches(settings)
        with a, b:
            runner.launch("job-123", Path("/app/jobs/job-123"))

        env = cast(dict[str, str], _request(client)["env"])
        assert env["JOB_DIR"] == f"{AGENT_APP_DIR}/jobs/job-123"
        assert env["OPENSCIENTIST_HOST_PROJECT_DIR"] == "/host/project"
        assert env["OPENSCIENTIST_CONTAINER_APP_DIR"] == AGENT_APP_DIR
        assert _request(client)["pids_limit"] == 256

    def test_launch_sends_no_fields_the_launcher_chooses(self):
        """The web may choose the job, the mode, the allowed env and resources;
        the image, mounts, network, labels, user and capabilities are the
        launcher's, and never appear in the request."""
        runner, client = _runner_with_client()
        a, b = self._launch_patches(self._make_settings(host_project_dir=None))
        with a, b:
            runner.launch("job-123", Path("/app/jobs/job-123"))

        request = _request(client)
        for field in (
            "image",
            "volumes",
            "network",
            "labels",
            "user",
            "cap_add",
            "entrypoint",
            "extra_hosts",
            "platform",
            "privileged",
            "security_opt",
            "stop_signal",
            "name",
        ):
            assert field not in request, f"the request carries {field}"

    def test_launch_omits_host_path_mapping_without_host_project_dir(self):
        """Launch omits host-path env vars when the host project path is unset."""
        runner, client = _runner_with_client()
        a, b = self._launch_patches(self._make_settings(host_project_dir=None))
        with a, b:
            runner.launch("job-123", Path("/app/jobs/job-123"))

        env = cast(dict[str, str], _request(client)["env"])
        assert "OPENSCIENTIST_HOST_PROJECT_DIR" not in env
        assert "OPENSCIENTIST_CONTAINER_APP_DIR" not in env

    def _launch_and_get_env(self, *, run_mode: str | None) -> dict[str, str]:
        """Run launch() (optionally with run_mode) and return the request env."""
        runner, client = _runner_with_client()
        a, b = self._launch_patches(self._make_settings(host_project_dir=None))
        with a, b:
            if run_mode is None:
                runner.launch("job-123", Path("/app/jobs/job-123"))
            else:
                runner.launch("job-123", Path("/app/jobs/job-123"), run_mode=run_mode)
        return cast(dict[str, str], _request(client)["env"])

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
        runner, client = _runner_with_client()
        client.list_containers.return_value = [_listed_entry({})]
        client.inspect.return_value = {"status": "running", "exit_code": None}
        assert runner.get_exit_code("job-123") is None
        client.list_containers.assert_called_once_with(
            ["openscientist.job_id=job-123", "openscientist.type=agent"]
        )

    def test_get_exit_code_returns_the_code_of_an_exited_container(self):
        runner, client = _runner_with_client()
        client.list_containers.return_value = [_listed_entry({})]
        client.inspect.return_value = {"status": "exited", "exit_code": 3}
        assert runner.get_exit_code("job-123") == 3

    def test_get_exit_code_returns_none_when_container_disappears(self):
        """Exit-code polling treats a vanished container as a benign miss."""
        runner, client = _runner_with_client()
        client.list_containers.return_value = [_listed_entry({})]
        client.inspect.side_effect = LauncherNotFoundError("not_found", "gone")
        with patch("openscientist.job_container.runner.logger.warning") as mock_warning:
            exit_code = runner.get_exit_code("job-123")
        assert exit_code is None
        mock_warning.assert_not_called()


class TestPhenixMount:
    """Phenix is advertised and mounted through the launcher's fixed path, only
    when the operator gives the launcher a host path."""

    @staticmethod
    def _settings(*, phenix_host_path: str | None) -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        provider.codex_auth_host_path = None
        provider.gcp_credentials_host_path = None
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=None, container_app_dir="/app", agent_pids_limit=512
            ),
            provider=provider,
            database=SimpleNamespace(effective_database_url="postgresql://db"),
            phenix=SimpleNamespace(phenix_host_path=phenix_host_path),
            airgap=SimpleNamespace(enabled=False),
            secret_key="master-key",
        )

    def _launch(self, *, phenix_host_path: str | None) -> dict[str, object]:
        runner, client = _runner_with_client()
        settings = self._settings(phenix_host_path=phenix_host_path)
        with (
            patch("openscientist.job_container.runner.get_settings", return_value=settings),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(RUN_HOME=None),
            ),
        ):
            runner.launch("job-123", Path("/app/jobs/job-123"))
        return _request(client)

    def test_phenix_mounted_when_available(self):
        """The configured Linux Phenix path is mounted into the agent container
        by the launcher, which the web enables with the flag and the env."""
        request = self._launch(phenix_host_path="/Applications/phenix-1.21.2")
        assert request["phenix"] is True
        env = cast(dict[str, str], request["env"])
        assert env["PHENIX_PATH"] == "/opt/phenix"

    def test_phenix_not_mounted_without_host_path(self):
        """Phenix is not mounted when phenix_host_path is unset."""
        request = self._launch(phenix_host_path=None)
        assert request["phenix"] is False
        assert "PHENIX_PATH" not in cast(dict[str, str], request["env"])


class TestJobSecretInjection:
    """The job container receives a per-job derived secret, never the master."""

    @staticmethod
    def _settings(master: str = "master-key") -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=None, container_app_dir="/app", agent_pids_limit=512
            ),
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


class TestFirewallLaunch:
    """The job container runs behind the nftables egress firewall in every
    mode, air-gapped or not: the web sends the flag and the structured
    host:port entries, and the launcher formats the allow list and applies
    the firewall trio itself."""

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
                agent_pids_limit=512,
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
        """Launch with the row's airgap flag, and return the launcher request."""
        await set_admin_settings_row(test_engine, airgapped=airgapped)
        runner, client = _runner_with_client()
        with (
            patch(
                "openscientist.job_container.runner.get_settings",
                return_value=self._settings(provider_id=provider_id),
            ),
            patch(
                "openscientist.job_container.runner.get_provider",
                return_value=_FakeProvider(),
            ),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(RUN_HOME=None),
            ),
        ):
            runner = JobContainerRunner()
            runner._client = client
            await asyncio.to_thread(runner.launch, "job-123", Path("/app/jobs/job-123"))
        return cast(dict[str, object], client.create_agent.call_args.args[0])

    @pytest.mark.asyncio
    async def test_airgap_launch_sends_the_structured_allowlist(
        self, test_engine: AsyncEngine
    ) -> None:
        request = await self._launch(test_engine, airgapped=True)
        assert request["airgapped"] is True
        entries = set(cast(list[str], request["egress_allow"]))
        assert "postgres:5432" in entries
        assert "openscientist:8082" in entries
        # Ollama is a proxied provider now, so the container reaches the proxy,
        # not the model server directly.
        assert "openscientist:8081" in entries
        assert "host.docker.internal:11434" not in entries
        env = cast(dict[str, str], request["env"])
        # The launcher formats the allow list; the web never sends it.
        assert "OPENSCIENTIST_FIREWALL_ALLOW" not in env

    @pytest.mark.asyncio
    async def test_restricted_launch_sends_the_web_allowlist(
        self, test_engine: AsyncEngine
    ) -> None:
        """Without air-gap the firewall allowlists only the web's services, so
        no provider posture is consulted."""
        request = await self._launch(test_engine, airgapped=False)
        assert request["airgapped"] is False
        assert set(cast(list[str], request["egress_allow"])) == {
            "postgres:5432",
            "openscientist:8081",
            "openscientist:8082",
        }


class TestChatTurnLaunch:
    """In-page chat runs one turn in an ephemeral, hardened container."""

    @staticmethod
    def _settings() -> SimpleNamespace:
        provider = MagicMock()
        provider.get_container_env_vars.return_value = {}
        provider.codex_auth_host_path = None
        provider.google_application_credentials = None
        provider.gcp_credentials_host_path = None
        provider.provider_id = "anthropic"
        return SimpleNamespace(
            container=SimpleNamespace(
                host_project_dir=None,
                container_app_dir="/app",
                agent_pids_limit=512,
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
        *,
        exit_code: int | None = 0,
        timed_out: bool = False,
        stop_error: Exception | None = None,
    ) -> tuple[JobContainerRunner, MagicMock]:
        runner, client = _runner_with_client()
        client.wait.return_value = {"exit_code": exit_code, "timeout": timed_out}
        client.logs.return_value = "container log"
        if stop_error is not None:
            client.stop.side_effect = stop_error
        with (
            patch("openscientist.job_container.runner.get_settings", return_value=self._settings()),
            patch(
                "openscientist.agent.factory.agent_class_for_provider_id",
                return_value=MagicMock(RUN_HOME=None),
            ),
        ):
            runner.run_chat_turn("job-123", Path("/app/jobs/job-123"), "t1")
        return runner, client

    def test_chat_turn_launches_hardened_container(self) -> None:
        _, client = self._run_chat()
        request = _request(client)
        assert request["mode"] == "chat"
        assert request["chat_turn"] == "t1"
        env = cast(dict[str, str], request["env"])
        # The chat container carries the per-job derived secret, never the master.
        assert env["OPENSCIENTIST_SECRET_KEY"] != "master-key"
        assert env["OPENSCIENTIST_RUN_MODE"] == "chat"
        client.wait.assert_called_once_with("c" * 64, timeout_seconds=300)
        # Stopped with the grace a turn needs to write its cost, then removed.
        calls = [c for c in client.method_calls if c[0] in ("stop", "remove")]
        assert calls == [
            call.stop("c" * 64, timeout_seconds=_STOP_GRACE_SECONDS),
            call.remove("c" * 64),
        ]

    def test_chat_turn_raises_on_nonzero_exit(self) -> None:
        with pytest.raises(RuntimeError, match="exited with code 1"):
            self._run_chat(exit_code=1)

    def test_chat_turn_raises_when_the_wait_times_out(self) -> None:
        """A turn cut at its bound still gets its cost written by the stop."""
        with pytest.raises(RuntimeError, match="did not finish"):
            self._run_chat(timed_out=True)

    def test_chat_turn_removes_container_and_drops_login_when_stop_fails(
        self,
        agent_login: SimpleNamespace,
    ) -> None:
        _, client = self._run_chat(
            stop_error=LauncherDockerError("docker_error", "stop failed"),
        )
        # A failed stop must not lose the cleanup either.
        client.remove.assert_called_once_with("c" * 64)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

    def test_chat_turn_tolerates_stop_when_container_is_gone(
        self,
        agent_login: SimpleNamespace,
    ) -> None:
        _, client = self._run_chat(stop_error=LauncherNotFoundError("not_found", "gone"))
        # A container already gone is swallowed silently, cleanup still runs.
        client.remove.assert_called_once_with("c" * 64)
        agent_login.closed.assert_called_once_with(_LOGIN.role)

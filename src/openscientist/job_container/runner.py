"""
JobContainerRunner — launches and manages per-job agent containers through the
container launcher.

Each agent job runs in its own ephemeral Docker container for security
isolation. The container:
- Runs the openscientist-agent image (contains claude-agent-sdk + Node.js)
- Mounts the job directory as /agent/jobs/<job_id>
- Receives provider credentials via env vars
- Communicates status back to the web server via PostgreSQL only, through its
  own database login (see ``db_login``)

The web holds no Docker client: every container operation goes through the
launcher, the only holder of the Docker socket, which builds the container
from its agent template and answers a uniform 404 for anything it did not
create itself.

Usage::

    runner = JobContainerRunner()
    handle = runner.launch(job_id, job_dir)
    # ... later ...
    runner.cleanup(job_id)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from openscientist.admin_settings import AdminSettingsValues, current_admin_settings_sync
from openscientist.database.retry import CONTAINER_DB_RETRY_SECONDS
from openscientist.exceptions import MissingImageError
from openscientist.exec_broker_client import (
    EXEC_BROKER_URL_ENV,
    EXEC_TOKEN_ENV,
    container_broker_base_url,
)
from openscientist.job_container.db_login import (
    AGENT_LOGIN_LABEL,
    close_agent_login,
    open_agent_login,
)
from openscientist.job_container.launcher_client import (
    ContainerEntry,
    CreateAgentResponse,
    LauncherClient,
    LauncherError,
    LauncherImageMissingError,
    LauncherNotFoundError,
)
from openscientist.job_container.run_home import (
    RUN_HOME_LABEL,
    create_run_home,
    remove_run_home,
)
from openscientist.job_container.secrets import (
    RunCredentials,
    derive_job_secret,
    issue_run_credentials,
)
from openscientist.job_container.utils import HostPathSettings, to_container_path, to_host_path
from openscientist.llm_proxy import container_proxy_base_url
from openscientist.providers import get_provider
from openscientist.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Seconds a stopped agent gets, after SIGTERM, to write the cost of the
#: iteration in progress before the launcher's stop turns into a kill.
_STOP_GRACE_SECONDS = 10

AGENT_APP_DIR = "/agent"
AGENT_GCP_CREDENTIALS_PATH = f"{AGENT_APP_DIR}/gcp-credentials.json"


class JobContainerRunner:
    """Launches and stops per-job agent containers through the launcher."""

    def __init__(self) -> None:
        self._client: LauncherClient | None = None

    @property
    def client(self) -> LauncherClient:
        """The launcher client, built on first use."""
        if self._client is None:
            self._client = LauncherClient()
        return self._client

    @staticmethod
    def _build_container_environment(
        settings: Settings,
        *,
        job_id: str,
        job_mount: str,
        provider_env: dict[str, str],
        database_url: str,
        exec_token: str,
        airgapped: bool,
        run_mode: str = "discovery",
    ) -> dict[str, str]:
        """Build the environment variables for the agent container."""
        cs = settings.container
        # Inject a per-job derived secret, never the master key (untrusted container).
        env: dict[str, str] = {
            "JOB_ID": job_id,
            "JOB_DIR": job_mount,
            # The container's own login, never the web's.
            "DATABASE_URL": database_url,
            # A short database outage should not end the job.
            "OPENSCIENTIST_DB_CONNECT_RETRY_SECONDS": str(CONTAINER_DB_RETRY_SECONDS),
            "OPENSCIENTIST_SECRET_KEY": derive_job_secret(settings.secret_key, job_id),
            # This run's execution credential the broker verifies, plus the broker URL.
            EXEC_TOKEN_ENV: exec_token,
            EXEC_BROKER_URL_ENV: container_broker_base_url(),
            **provider_env,
        }
        # Only set the run-mode override when it diverges from the default so
        # ordinary discovery launches keep a clean env. The entrypoint reads
        # OPENSCIENTIST_RUN_MODE. "report_only" re-runs just the report phase.
        if run_mode != "discovery":
            env["OPENSCIENTIST_RUN_MODE"] = run_mode
        # Forward the per-turn Codex timeout so the agent (CodexAgent reads
        # OPENSCIENTIST_CODEX_TURN_TIMEOUT at import) can be tuned for slow
        # local backends. Without this the agent always uses the 900s default.
        turn_timeout = os.environ.get("OPENSCIENTIST_CODEX_TURN_TIMEOUT")
        if turn_timeout:
            env["OPENSCIENTIST_CODEX_TURN_TIMEOUT"] = turn_timeout
        if cs.host_project_dir:
            env["OPENSCIENTIST_HOST_PROJECT_DIR"] = cs.host_project_dir
            env["OPENSCIENTIST_CONTAINER_APP_DIR"] = AGENT_APP_DIR
        # Air-gapped mode routes the tools subprocess to the local PubMed corpus.
        if airgapped:
            env["OPENSCIENTIST_AIRGAPPED"] = "1"
        if settings.phenix.phenix_host_path:
            env["PHENIX_PATH"] = "/opt/phenix"
        return env

    @staticmethod
    def _provider_environment(settings: Settings, credentials: RunCredentials) -> dict[str, str]:
        """The provider's env for the container, plus the self-hosted window."""
        gcp_credentials_container_path = (
            AGENT_GCP_CREDENTIALS_PATH if settings.provider.gcp_credentials_host_path else None
        )
        provider = get_provider()
        env = provider.proxied_container_env(
            proxy_base_url=container_proxy_base_url(),
            placeholder=credentials.llm_placeholder,
            gcp_credentials_container_path=gcp_credentials_container_path,
        )
        # Resolve a self-hosted model's window app-side and pass it in, since the
        # proxied container cannot probe a root path like llama.cpp's /props.
        env.update(provider.prelaunch_model_context_env())
        return env

    @staticmethod
    def _egress_allow(settings: Settings, airgapped: bool) -> list[str]:
        """The structured host:port entries the launcher formats into the
        firewall allow list, or none when not air-gapped."""
        if not airgapped:
            return []
        from openscientist.job_container.egress import derive_egress_allowlist

        posture = get_provider().airgap_egress()
        entries = derive_egress_allowlist(settings, posture)
        return [f"{host}:{port}" for host, port in entries]

    def launch(
        self, job_id: str, job_dir: Path, *, run_mode: str = "discovery"
    ) -> CreateAgentResponse:
        """
        Launch an agent container for the given job.

        The container runs docker/agent-entrypoint.py which calls
        run_discovery_async(job_dir), or regenerate_report_async(job_dir) when
        run_mode is "report_only".

        Args:
            job_id: Job UUID string
            job_dir: Absolute path to the job directory on the web's side
            run_mode: "discovery" (full loop) or "report_only" (report phase
                only, against the already-persisted findings)

        Returns:
            The launcher's handle of the created container: id, short_id,
            name and status

        Raises:
            MissingImageError: If the launcher's agent image is not on the host
        """
        handle, _ = self._start_agent_container(
            job_id=job_id,
            job_dir=job_dir,
            run_mode=run_mode,
            lifetime_seconds=current_admin_settings_sync().agent_timeout_seconds,
        )
        logger.info("Launched agent container %s for job %s", handle["short_id"], job_id)
        return handle

    def _start_agent_container(
        self,
        *,
        job_id: str,
        job_dir: Path,
        run_mode: str,
        lifetime_seconds: int,
        chat_turn: str | None = None,
    ) -> tuple[CreateAgentResponse, dict[str, str]]:
        """Create one agent or chat container through the launcher, and return
        its handle with the labels the launcher will set on it.

        The container gets a database login of its own, created here and named
        in its labels, so whoever removes the container can drop the login. A
        harness with a home gets one for this run alone, outside the job
        directory, named in the labels too, and removed with the login."""
        settings: Settings = get_settings()
        admin = current_admin_settings_sync()

        # The launcher derives the job mount from the job id alone and
        # validates it against the host's jobs root.
        job_dir_resolved = job_dir.resolve()
        from openscientist.agent.factory import agent_class_for_provider_id

        agent_class = agent_class_for_provider_id(settings.provider.provider_id)
        login = open_agent_login(job_id, lifetime_seconds=lifetime_seconds)
        run_home: Path | None = None
        try:
            if agent_class.RUN_HOME:
                run_home = create_run_home(job_dir_resolved.parent, login.role)
                agent_class.provision_run_home(settings, run_home)
            # Ended with the login, when the container goes.
            credentials = issue_run_credentials(job_id, login.role)
            handle, labels = self._run_container(
                settings,
                admin,
                job_id=job_id,
                run_mode=run_mode,
                lifetime_seconds=lifetime_seconds,
                chat_turn=chat_turn,
                database_url=login.database_url,
                login_role=login.role,
                credentials=credentials,
                run_home=(run_home, agent_class.RUN_HOME)
                if run_home and agent_class.RUN_HOME
                else None,
            )
            return handle, labels
        except BaseException:
            if run_home is not None:
                remove_run_home(run_home)
            close_agent_login(login.role)
            raise

    def _run_container(
        self,
        settings: Settings,
        admin: AdminSettingsValues,
        *,
        job_id: str,
        run_mode: str,
        lifetime_seconds: int,
        chat_turn: str | None,
        database_url: str,
        login_role: str,
        credentials: RunCredentials,
        run_home: tuple[Path, str] | None,
    ) -> tuple[CreateAgentResponse, dict[str, str]]:
        """Build the agent request the launcher validates, and create the
        container from its template. The name, mounts, network and firewall
        overrides are the launcher's to choose."""
        cs = settings.container
        env = self._build_container_environment(
            settings,
            job_id=job_id,
            job_mount=f"{AGENT_APP_DIR}/jobs/{job_id}",
            provider_env=self._provider_environment(settings, credentials),
            database_url=database_url,
            exec_token=credentials.exec_token,
            airgapped=admin.airgapped,
            run_mode=run_mode,
        )
        labels: dict[str, str] = {
            "openscientist.job_id": job_id,
            "openscientist.type": "chat" if run_mode == "chat" else "agent",
            AGENT_LOGIN_LABEL: login_role,
        }
        request: dict[str, object] = {
            "job_id": job_id,
            "mode": run_mode,
            "lifetime_seconds": lifetime_seconds,
            "env": env,
            "db_login": login_role,
            "gcp_credentials": bool(settings.provider.gcp_credentials_host_path),
            "phenix": bool(settings.phenix.phenix_host_path),
            "memory_mib": admin.agent_memory_mib,
            "cpu": admin.agent_cpu,
            "airgapped": admin.airgapped,
        }
        # The launcher refuses an egress list on an open network, so the list
        # travels only with the airgapped flag it configures.
        if admin.airgapped:
            request["egress_allow"] = self._egress_allow(settings, True)
        if chat_turn is not None:
            request["chat_turn"] = chat_turn
        if run_home is not None:
            home, name_in_job = run_home
            host_home = to_host_path(home, cs)
            labels[RUN_HOME_LABEL] = str(host_home)
            request["run_home"] = {"path": str(host_home), "name": name_in_job}
        try:
            return self.client.create_agent(request), labels
        except LauncherImageMissingError as error:
            raise MissingImageError(cs.agent_image) from error

    def run_chat_turn(
        self, job_id: str, job_dir: Path, chat_turn: str, *, timeout: int = 300
    ) -> None:
        """Run one chat turn in an ephemeral hardened container and wait for it.

        Inherits the job launch posture. Prompt and reply cross through the
        turn's files in job_dir, not the database. Raises on timeout or non-zero
        exit, and the container is always removed."""
        handle, labels = self._start_agent_container(
            job_id=job_id,
            job_dir=job_dir,
            run_mode="chat",
            lifetime_seconds=timeout,
            chat_turn=chat_turn,
        )
        ref = handle["id"]
        try:
            try:
                outcome = self.client.wait(ref, timeout_seconds=timeout)
            except LauncherError as error:
                raise RuntimeError(f"Chat turn did not finish within {timeout}s") from error
            if outcome["timeout"]:
                raise RuntimeError(f"Chat turn did not finish within {timeout}s")
            exit_code = outcome["exit_code"]
            if exit_code is None:
                exit_code = 1
            if exit_code != 0:
                logs = self.client.logs(ref)
                raise RuntimeError(f"Chat container exited with code {exit_code}: {logs[-2000:]}")
        finally:
            try:
                # A turn cut at the time limit writes its cost when stopped, so
                # a forced removal would lose it.
                self.client.stop(ref, timeout_seconds=_STOP_GRACE_SECONDS)
            except LauncherNotFoundError:
                pass
            except LauncherError as error:
                logger.warning("Failed to stop chat container %s: %s", handle["name"], error)
            try:
                self.client.remove(ref)
            except LauncherNotFoundError:
                pass
            except LauncherError as error:
                logger.warning("Failed to remove chat container %s: %s", handle["name"], error)
            self._end_run_of(labels, get_settings().container)

    @staticmethod
    def _end_run_of(labels: Mapping[str, str], cs: HostPathSettings) -> None:
        """Remove the run's home and drop its database login, both named in the
        container's labels. The home is the launcher's host path, so it comes
        back to the web's side before removal."""
        home = labels.get(RUN_HOME_LABEL)
        if home:
            remove_run_home(to_container_path(Path(home), cs))
        role = labels.get(AGENT_LOGIN_LABEL)
        if role:
            close_agent_login(role)

    def stop(self, job_id: str, timeout: int = _STOP_GRACE_SECONDS) -> None:
        """Stop the container for a job (graceful, then a kill)."""
        entry = self._find_container(job_id)
        if entry:
            try:
                self.client.stop(entry["id"], timeout_seconds=timeout)
                logger.info("Stopped container for job %s", job_id)
            except LauncherNotFoundError:
                return
            except LauncherError as error:
                logger.warning("Failed to stop container for job %s: %s", job_id, error)

    def cleanup(self, job_id: str, log_dir: Path | None = None) -> None:
        """Stop the container for a job, giving it the same grace as :meth:`stop`,
        then save its logs if asked, remove it and drop its database login."""
        entry = self._find_container(job_id)
        if entry:
            try:
                # The agent writes the cost of the iteration in progress when it
                # is stopped, so a forced removal would lose it.
                try:
                    self.client.stop(entry["id"], timeout_seconds=_STOP_GRACE_SECONDS)
                except LauncherNotFoundError:
                    pass
                except LauncherError as error:
                    logger.warning("Failed to stop container for job %s: %s", job_id, error)
                if log_dir is not None:
                    try:
                        logs = self.client.logs(entry["id"])
                        (log_dir / "agent-container.log").write_text(logs)
                    except (LauncherError, OSError) as error:
                        if not isinstance(error, LauncherNotFoundError):
                            logger.warning(
                                "Failed to save container logs for job %s: %s",
                                job_id,
                                error,
                            )
                self.client.remove(entry["id"])
                logger.info("Removed container for job %s", job_id)
            except LauncherNotFoundError:
                pass
            except LauncherError as error:
                logger.warning("Failed to remove container for job %s: %s", job_id, error)
            # Done even when removal failed, so a container that lingers loses
            # its database access and its copy of the operator's login.
            self._end_run_of(entry["labels"], get_settings().container)

    def get_exit_code(self, job_id: str) -> int | None:
        """
        Return the exit code of the agent container if it has stopped, else None.

        Returns None if the container is still running or cannot be found.
        """
        entry = self._find_container(job_id)
        if entry is None:
            return None
        try:
            result = self.client.inspect(entry["id"])
        except LauncherNotFoundError:
            return None
        except LauncherError as error:
            logger.warning("Failed to get exit code for job %s: %s", job_id, error)
            return None
        if result["status"] in ("exited", "dead"):
            exit_code = result["exit_code"]
            if isinstance(exit_code, int):
                return exit_code
            if exit_code is not None:
                try:
                    return int(exit_code)
                except (TypeError, ValueError):
                    logger.warning(
                        "Unexpected non-integer exit code for job %s: %r",
                        job_id,
                        exit_code,
                    )
        return None

    def _listed_labels(self) -> list[dict[str, str]] | None:
        """Labels of every agent and chat container the launcher lists, running
        or exited, or None when the launcher cannot be asked."""
        try:
            entries = self.client.list_containers([AGENT_LOGIN_LABEL])
        except LauncherError as error:
            logger.warning("Failed to list agent containers: %s", error)
            return None
        return [entry["labels"] for entry in entries]

    def live_login_roles(self) -> set[str] | None:
        """Database logins of every agent and chat container the launcher lists,
        running or exited, or None when the launcher cannot be asked."""
        listed = self._listed_labels()
        if listed is None:
            return None
        return {role for labels in listed if (role := labels.get(AGENT_LOGIN_LABEL))}

    def live_job_ids(self) -> set[str] | None:
        """Jobs of every agent and chat container the launcher lists, running or
        exited, or None when the launcher cannot be asked."""
        listed = self._listed_labels()
        if listed is None:
            return None
        return {job for labels in listed if (job := labels.get("openscientist.job_id"))}

    def _find_container(self, job_id: str) -> ContainerEntry | None:
        """The launcher's entry of the agent container for a job, or None."""
        try:
            entries = self.client.list_containers(
                [f"openscientist.job_id={job_id}", "openscientist.type=agent"]
            )
            return entries[0] if entries else None
        except LauncherError as error:
            logger.warning("Failed to find container for job %s: %s", job_id, error)
            return None

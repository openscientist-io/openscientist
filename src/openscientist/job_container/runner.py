"""
JobContainerRunner — launches and manages per-job Docker containers.

Each agent job runs in its own ephemeral Docker container for security
isolation.  The container:
- Runs the openscientist-agent image (contains claude-agent-sdk + Node.js)
- Mounts the job directory as /agent/jobs/<job_id>
- Receives provider credentials via env vars
- Communicates status back to the web server via PostgreSQL only, through its
  own database login (see ``db_login``)

Usage::

    runner = JobContainerRunner()
    container = runner.launch(job_id, job_dir)
    # ... later ...
    runner.cleanup(job_id)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from docker.models.containers import Container

import docker
from docker import errors as docker_errors
from openscientist.admin_settings import AdminSettingsValues, current_admin_settings_sync
from openscientist.database.retry import CONTAINER_DB_RETRY_SECONDS
from openscientist.exceptions import MissingImageError
from openscientist.exec_broker_client import (
    EXEC_BROKER_URL_ENV,
    EXEC_TOKEN_ENV,
    container_broker_base_url,
)
from openscientist.job_chat import CHAT_TURN_ENV
from openscientist.job_container.db_login import (
    AGENT_LOGIN_LABEL,
    close_agent_login,
    open_agent_login,
)
from openscientist.job_container.identity import apply_job_ownership
from openscientist.job_container.run_home import (
    RUN_HOME_LABEL,
    create_run_home,
    remove_run_home,
)
from openscientist.job_container.secrets import (
    RunCredentials,
    RunKind,
    derive_job_secret,
    issue_run_credentials,
)
from openscientist.job_container.utils import resolve_docker_network, to_host_path
from openscientist.llm_proxy import container_proxy_base_url
from openscientist.providers import get_provider
from openscientist.settings import Settings, get_settings
from openscientist.version import SHORT_COMMIT_LENGTH

logger = logging.getLogger(__name__)

#: Seconds a stopped agent gets, after SIGTERM, to write the cost of the
#: iteration in progress before docker sends SIGKILL.
_STOP_GRACE_SECONDS = 10

AGENT_APP_DIR = "/agent"


class JobContainerRunner:
    """Launches and stops per-job agent containers."""

    def __init__(self) -> None:
        self._docker: docker.DockerClient = docker.from_env()

    @staticmethod
    def _is_not_found_error(error: Exception) -> bool:
        """Return True when Docker reports that a container no longer exists."""
        return isinstance(error, docker_errors.NotFound)

    def _get_network(self, configured_network: str | None) -> str:
        """Resolve the Docker network for agent containers."""
        return resolve_docker_network(self._docker, configured_network)

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
    def _build_container_volumes(
        settings: Settings,
        *,
        job_dir_host: Path,
        job_mount: str,
    ) -> dict[str, dict[str, str]]:
        """Build the bind mounts for the agent container."""
        volumes: dict[str, dict[str, str]] = {
            str(job_dir_host): {"bind": job_mount, "mode": "rw"},
        }
        phenix_host = settings.phenix.phenix_host_path
        if phenix_host:
            volumes[str(Path(phenix_host).expanduser().resolve())] = {
                "bind": "/opt/phenix",
                "mode": "ro",
            }
        return volumes

    @staticmethod
    def _agent_runtime_settings(
        settings: Settings,
        admin: AdminSettingsValues,
    ) -> tuple[str | None, str, float, str | None]:
        """Return network, memory, CPU, and platform settings for the agent."""
        container_settings = settings.container
        return (
            container_settings.agent_network,
            f"{admin.agent_memory_mib}m",
            admin.agent_cpu,
            container_settings.agent_platform,
        )

    @staticmethod
    def _build_launch_configuration(
        settings: Settings,
        admin: AdminSettingsValues,
        *,
        job_id: str,
        job_dir_host: Path,
        database_url: str,
        credentials: RunCredentials,
        run_mode: str = "discovery",
    ) -> tuple[
        dict[str, str],
        dict[str, dict[str, str]],
        str | None,
        str,
        float,
        str | None,
    ]:
        """Build the environment, mounts, and runtime settings for launch()."""
        agent_network, agent_memory, agent_cpu, agent_platform = (
            JobContainerRunner._agent_runtime_settings(settings, admin)
        )
        job_mount = f"{AGENT_APP_DIR}/jobs/{job_id}"
        provider = get_provider()
        routing_error = provider.prelaunch_routing_error()
        if routing_error is not None:
            raise RuntimeError(routing_error)
        provider_env = provider.proxied_container_env(
            proxy_base_url=container_proxy_base_url(),
            placeholder=credentials.llm_placeholder,
        )
        # Resolve a self-hosted model's window app-side and pass it in, since the
        # proxied container cannot probe a root path like llama.cpp's /props.
        provider_env.update(provider.prelaunch_model_context_env())
        env = JobContainerRunner._build_container_environment(
            settings,
            job_id=job_id,
            job_mount=job_mount,
            provider_env=provider_env,
            database_url=database_url,
            exec_token=credentials.exec_token,
            airgapped=admin.airgapped,
            run_mode=run_mode,
        )
        volumes = JobContainerRunner._build_container_volumes(
            settings, job_dir_host=job_dir_host, job_mount=job_mount
        )
        return env, volumes, agent_network, agent_memory, agent_cpu, agent_platform

    @staticmethod
    def _airgap_firewall_config(
        settings: Settings,
        airgapped: bool,
    ) -> tuple[list[str] | None, str | None, list[str] | None, dict[str, str]]:
        """Firewall launch overrides (cap_add, user, entrypoint, extra_env) for
        air-gapped mode, or neutral values when off."""
        if not airgapped:
            return None, None, None, {}
        from openscientist.job_container.egress import (
            derive_egress_allowlist,
            format_egress_allowlist,
        )

        posture = get_provider().airgap_egress()
        allow = format_egress_allowlist(derive_egress_allowlist(settings, posture))
        return (
            ["NET_ADMIN"],
            "root",
            ["/agent-firewall-entrypoint.sh"],
            {"OPENSCIENTIST_FIREWALL_ALLOW": allow},
        )

    def launch(self, job_id: str, job_dir: Path, *, run_mode: str = "discovery") -> Container:
        """
        Launch an agent container for the given job.

        The container runs docker/agent-entrypoint.py which calls
        run_discovery_async(job_dir), or regenerate_report_async(job_dir) when
        run_mode is "report_only".

        Args:
            job_id: Job UUID string (used for container name + labels)
            job_dir: Absolute host path to the job directory
            run_mode: "discovery" (full loop) or "report_only" (report phase
                only, against the already-persisted findings)

        Returns:
            docker.models.containers.Container object

        Raises:
            RuntimeError: If Docker is unavailable or launch fails
        """
        container = self._start_agent_container(
            job_id=job_id,
            job_dir=job_dir,
            run_mode=run_mode,
            name=f"openscientist-agent-{job_id[:SHORT_COMMIT_LENGTH]}",
            container_type="agent",
            lifetime_seconds=current_admin_settings_sync().agent_timeout_seconds,
        )
        logger.info("Launched agent container %s for job %s", container.short_id, job_id)
        return container

    def _start_agent_container(
        self,
        *,
        job_id: str,
        job_dir: Path,
        run_mode: str,
        name: str,
        container_type: str,
        lifetime_seconds: int,
        chat_turn: str | None = None,
    ) -> Container:
        """Build the hardened launch config and start a detached agent container.
        Shared by discovery/report launches and one-off chat turns.

        The container gets a database login of its own, created here and named
        in its labels, so whoever removes the container can drop the login. A
        harness with a home gets one for this run alone, outside the job
        directory, named in the labels too, and removed with the login."""
        settings: Settings = get_settings()
        admin = current_admin_settings_sync()
        cs = settings.container

        # Translate job_dir to a host-absolute path: resolve first so a relative
        # path becomes container-absolute, then map to the host (Docker bind
        # mounts require host-absolute paths).
        job_dir_resolved = job_dir.resolve()
        from openscientist.agent.factory import agent_class_for_provider_id

        agent_class = agent_class_for_provider_id(settings.provider.provider_id)
        job_dir_host = to_host_path(job_dir_resolved, cs)
        login = open_agent_login(job_id, lifetime_seconds=lifetime_seconds)
        run_home: Path | None = None
        try:
            if agent_class.RUN_HOME:
                run_home = create_run_home(job_dir_resolved.parent, login.role)
                agent_class.provision_run_home(settings, run_home)
            # Ended with the login, when the container goes. The kind records
            # the run's work so the proxy can attribute its forwarded calls.
            run_kind: RunKind = (
                "chat"
                if run_mode == "chat"
                else "report"
                if run_mode == "report_only"
                else "discovery"
            )
            credentials = issue_run_credentials(job_id, login.role, run_kind)
            return self._run_container(
                settings,
                admin,
                job_id=job_id,
                job_dir_host=job_dir_host,
                run_mode=run_mode,
                name=name,
                container_type=container_type,
                database_url=login.database_url,
                login_role=login.role,
                chat_turn=chat_turn,
                credentials=credentials,
                run_home=(run_home, agent_class.RUN_HOME)
                if run_home and agent_class.RUN_HOME
                else None,
            )
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
        job_dir_host: Path,
        run_mode: str,
        name: str,
        container_type: str,
        database_url: str,
        login_role: str,
        chat_turn: str | None,
        credentials: RunCredentials,
        run_home: tuple[Path, str] | None,
    ) -> Container:
        cs = settings.container
        env, volumes, agent_network, agent_memory, agent_cpu, agent_platform = (
            self._build_launch_configuration(
                settings,
                admin,
                job_id=job_id,
                job_dir_host=job_dir_host,
                database_url=database_url,
                credentials=credentials,
                run_mode=run_mode,
            )
        )
        network = self._get_network(agent_network)
        cap_add, run_user, entrypoint, firewall_env = self._airgap_firewall_config(
            settings, admin.airgapped
        )
        env.update(firewall_env)
        if chat_turn is not None:
            env[CHAT_TURN_ENV] = chat_turn
        labels = {
            "openscientist.job_id": job_id,
            "openscientist.type": container_type,
            AGENT_LOGIN_LABEL: login_role,
        }
        if run_home is not None:
            home, name_in_job = run_home
            volumes[str(to_host_path(home, cs))] = {
                "bind": f"{AGENT_APP_DIR}/jobs/{job_id}/{name_in_job}",
                "mode": "rw",
            }
            labels[RUN_HOME_LABEL] = str(home)

        try:
            return self._docker.containers.run(
                image=cs.agent_image,
                name=name,
                detach=True,
                remove=False,
                environment=env,
                volumes=volumes,
                network=network,
                mem_limit=agent_memory,
                nano_cpus=int(agent_cpu * 1e9),
                platform=agent_platform or None,
                security_opt=["no-new-privileges:true"],
                cap_add=cap_add,
                user=run_user,
                entrypoint=entrypoint,
                # Map host.docker.internal to the host gateway so the container can
                # reach a model server on the host (e.g. a local Ollama). Harmless
                # otherwise. On Linux this is not provided by default.
                extra_hosts={"host.docker.internal": "host-gateway"},
                labels=labels,
            )
        except docker_errors.ImageNotFound as error:
            raise MissingImageError(cs.agent_image) from error

    def run_chat_turn(
        self, job_id: str, job_dir: Path, chat_turn: str, *, timeout: int = 300
    ) -> None:
        """Run one chat turn in an ephemeral hardened container and wait for it.

        Inherits the job launch posture. Prompt and reply cross through the
        turn's files in job_dir, not the database. Raises on timeout or non-zero
        exit, and the container is always removed."""
        name = f"openscientist-chat-{job_id[:SHORT_COMMIT_LENGTH]}-{os.urandom(4).hex()}"
        container = self._start_agent_container(
            job_id=job_id,
            job_dir=job_dir,
            run_mode="chat",
            name=name,
            container_type="chat",
            lifetime_seconds=timeout,
            chat_turn=chat_turn,
        )
        try:
            try:
                outcome = container.wait(timeout=timeout)
            except Exception as error:
                raise RuntimeError(f"Chat turn did not finish within {timeout}s") from error
            exit_code = int(outcome.get("StatusCode", 1)) if isinstance(outcome, dict) else 1
            if exit_code != 0:
                logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
                raise RuntimeError(f"Chat container exited with code {exit_code}: {logs[-2000:]}")
        finally:
            try:
                # A turn cut at the time limit writes its cost when stopped, so
                # a forced removal would lose it.
                container.stop(timeout=_STOP_GRACE_SECONDS)
            except docker_errors.APIError as error:
                if not self._is_not_found_error(error):
                    logger.warning("Failed to stop chat container %s: %s", name, error)
            try:
                container.remove(force=True)
            except docker_errors.APIError as error:
                if not self._is_not_found_error(error):
                    logger.warning("Failed to remove chat container %s: %s", name, error)
            self._end_run_of(container)

    @staticmethod
    def _end_run_of(container: Container) -> None:
        """Remove the run's home and drop its database login, both named in the
        container's labels."""
        home = container.labels.get(RUN_HOME_LABEL)
        if home:
            remove_run_home(Path(home))
        role = container.labels.get(AGENT_LOGIN_LABEL)
        if role:
            close_agent_login(role)

    def stop(self, job_id: str, timeout: int = _STOP_GRACE_SECONDS) -> None:
        """Stop the container for a job (graceful → SIGKILL)."""
        container = self._find_container(job_id)
        if container:
            try:
                container.stop(timeout=timeout)
                logger.info("Stopped container for job %s", job_id)
            except docker_errors.APIError as error:
                if self._is_not_found_error(error):
                    return
                logger.warning("Failed to stop container for job %s: %s", job_id, error)

    def cleanup(self, job_id: str, log_dir: Path | None = None) -> None:
        """Stop the container for a job, giving it the same grace as :meth:`stop`,
        then save its logs if asked, remove it and drop its database login."""
        container = self._find_container(job_id)
        if container:
            try:
                # The agent writes the cost of the iteration in progress when it
                # is stopped, so a forced removal would lose it.
                try:
                    container.stop(timeout=_STOP_GRACE_SECONDS)
                except docker_errors.APIError as error:
                    if not self._is_not_found_error(error):
                        logger.warning("Failed to stop container for job %s: %s", job_id, error)
                if log_dir is not None:
                    log_path = log_dir / "agent-container.log"
                    try:
                        logs = container.logs(stdout=True, stderr=True).decode(
                            "utf-8", errors="replace"
                        )
                        log_path.write_text(logs)
                        # Saved into the job dir, which the agent user owns.
                        apply_job_ownership(log_path)
                    except (docker_errors.APIError, OSError) as error:
                        if not self._is_not_found_error(error):
                            logger.warning(
                                "Failed to save container logs for job %s: %s",
                                job_id,
                                error,
                            )
                container.remove(force=True)
                logger.info("Removed container for job %s", job_id)
            except docker_errors.APIError as error:
                if not self._is_not_found_error(error):
                    logger.warning("Failed to remove container for job %s: %s", job_id, error)
            # Done even when removal failed, so a container that lingers loses
            # its database access and its copy of the operator's login.
            self._end_run_of(container)

    def get_exit_code(self, job_id: str) -> int | None:
        """
        Return the exit code of the agent container if it has stopped, else None.

        Returns None if the container is still running or cannot be found.
        """
        container = self._find_container(job_id)
        if container is None:
            return None
        try:
            container.reload()
            if container.status in ("exited", "dead"):
                exit_code = container.attrs.get("State", {}).get("ExitCode")
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
        except docker_errors.APIError as error:
            if self._is_not_found_error(error):
                return None
            logger.warning("Failed to get exit code for job %s: %s", job_id, error)
        return None

    def _listed_labels(self) -> list[dict[str, str]] | None:
        """Labels of every agent and chat container Docker lists, running or
        exited, or None when Docker cannot be asked."""
        try:
            containers = self._docker.containers.list(
                all=True, filters={"label": AGENT_LOGIN_LABEL}
            )
        except docker_errors.DockerException as error:
            logger.warning("Failed to list agent containers: %s", error)
            return None
        return [container.labels for container in containers]

    def live_login_roles(self) -> set[str] | None:
        """Database logins of every agent and chat container Docker lists,
        running or exited, or None when Docker cannot be asked."""
        listed = self._listed_labels()
        if listed is None:
            return None
        return {role for labels in listed if (role := labels.get(AGENT_LOGIN_LABEL))}

    def live_job_ids(self) -> set[str] | None:
        """Jobs of every agent and chat container Docker lists, running or
        exited, or None when Docker cannot be asked."""
        listed = self._listed_labels()
        if listed is None:
            return None
        return {job for labels in listed if (job := labels.get("openscientist.job_id"))}

    def _find_container(self, job_id: str) -> Container | None:
        """Find the agent container for a job by labels."""
        try:
            containers = self._docker.containers.list(
                all=True,
                filters={
                    "label": [
                        f"openscientist.job_id={job_id}",
                        "openscientist.type=agent",
                    ]
                },
            )
            return containers[0] if containers else None
        except docker_errors.DockerException as error:
            logger.warning("Failed to find container for job %s: %s", job_id, error)
            return None

"""
JobContainerRunner — launches and manages per-job Docker containers.

Each agent job runs in its own ephemeral Docker container for security
isolation.  The container:
- Runs the openscientist-agent image (contains claude-agent-sdk + Node.js)
- Mounts the job directory as /agent/jobs/<job_id>
- Receives provider credentials via env vars
- Communicates status back to the web server via PostgreSQL only

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
from typing import Any, cast

import docker
from docker import errors as docker_errors
from openscientist.job_container.utils import resolve_docker_network, to_host_path
from openscientist.settings import Settings, get_settings
from openscientist.version import SHORT_COMMIT_LENGTH

logger = logging.getLogger(__name__)

AGENT_IMAGE = "openscientist-agent:latest"
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
    ) -> dict[str, str]:
        """Build the environment variables for the agent container."""
        cs = settings.container
        provider_env = settings.provider.get_container_env_vars()
        env: dict[str, str] = {
            "JOB_ID": job_id,
            "JOB_DIR": job_mount,
            "DATABASE_URL": settings.database.effective_database_url,
            # Admin URL needed for RLS-protected tables (experts, review_tokens).
            "ADMIN_DATABASE_URL": settings.database.effective_admin_database_url,
            "OPENSCIENTIST_SECRET_KEY": settings.secret_key,
            **provider_env,
        }
        if cs.host_project_dir:
            env["OPENSCIENTIST_HOST_PROJECT_DIR"] = cs.host_project_dir
            env["OPENSCIENTIST_CONTAINER_APP_DIR"] = AGENT_APP_DIR
        if settings.provider.google_application_credentials:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = "/agent/gcp-credentials.json"
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
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
        }
        gcp_path = settings.provider.google_application_credentials
        if gcp_path:
            gcp_host_path = settings.provider.gcp_credentials_host_path or gcp_path
            volumes[str(gcp_host_path)] = {
                "bind": "/agent/gcp-credentials.json",
                "mode": "ro",
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
    ) -> tuple[str | None, str, float, str | None]:
        """Return network, memory, CPU, and platform settings for the agent."""
        container_settings = settings.container
        if hasattr(container_settings, "model_dump"):
            config = container_settings.model_dump()
        else:
            config = vars(container_settings)
        return (
            cast(str | None, config["agent_network"]),
            cast(str, config["agent_memory"]),
            cast(float, config["agent_cpu"]),
            cast(str | None, config["agent_platform"]),
        )

    @staticmethod
    def _build_launch_configuration(
        settings: Settings,
        *,
        job_id: str,
        job_dir_host: Path,
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
            JobContainerRunner._agent_runtime_settings(settings)
        )
        job_mount = f"{AGENT_APP_DIR}/jobs/{job_id}"
        env = JobContainerRunner._build_container_environment(
            settings, job_id=job_id, job_mount=job_mount
        )
        volumes = JobContainerRunner._build_container_volumes(
            settings, job_dir_host=job_dir_host, job_mount=job_mount
        )
        return env, volumes, agent_network, agent_memory, agent_cpu, agent_platform

    @staticmethod
    def _docker_socket_group() -> str | None:
        """Return the Docker socket GID when the socket is present."""
        socket_path = Path("/var/run/docker.sock")
        if not socket_path.exists():
            return None
        return str(os.stat(socket_path).st_gid)

    def launch(self, job_id: str, job_dir: Path) -> Any:
        """
        Launch an agent container for the given job.

        The container runs docker/agent-entrypoint.py which calls
        run_discovery_async(job_dir).

        Args:
            job_id: Job UUID string (used for container name + labels)
            job_dir: Absolute host path to the job directory

        Returns:
            docker.models.containers.Container object

        Raises:
            RuntimeError: If Docker is unavailable or launch fails
        """
        settings: Settings = get_settings()
        cs = settings.container

        # Translate job_dir from container-internal path to host path.
        # Must resolve to absolute FIRST (so relative paths like "jobs/uuid" become
        # "/app/jobs/uuid" inside the web container), then translate to the host
        # path.  Docker requires absolute paths for bind mounts; relative paths
        # are misinterpreted as named volumes.
        job_dir_host = to_host_path(job_dir.resolve(), cs)
        env, volumes, agent_network, agent_memory, agent_cpu, agent_platform = (
            self._build_launch_configuration(
                settings,
                job_id=job_id,
                job_dir_host=job_dir_host,
            )
        )
        network = self._get_network(agent_network)

        # We read the socket gid directly because the docker group may not exist
        # inside the web server container.
        docker_gid = self._docker_socket_group()
        container = self._docker.containers.run(
            image=AGENT_IMAGE,
            name=f"openscientist-agent-{job_id[:SHORT_COMMIT_LENGTH]}",
            detach=True,
            remove=False,
            environment=env,
            volumes=volumes,
            network=network,
            mem_limit=agent_memory,
            nano_cpus=int(agent_cpu * 1e9),
            platform=agent_platform or None,
            security_opt=["no-new-privileges:true"],
            group_add=[docker_gid] if docker_gid else [],
            labels={
                "openscientist.job_id": job_id,
                "openscientist.type": "agent",
            },
        )

        logger.info("Launched agent container %s for job %s", container.short_id, job_id)
        return container

    def stop(self, job_id: str, timeout: int = 10) -> None:
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
        """Remove the container for a job, optionally saving its logs first."""
        container = self._find_container(job_id)
        if container:
            try:
                if log_dir is not None:
                    try:
                        logs = container.logs(stdout=True, stderr=True).decode(
                            "utf-8", errors="replace"
                        )
                        (log_dir / "agent-container.log").write_text(logs)
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
                if self._is_not_found_error(error):
                    return
                logger.warning("Failed to remove container for job %s: %s", job_id, error)

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

    def _find_container(self, job_id: str) -> Any | None:
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

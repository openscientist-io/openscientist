"""
JobContainerRunner — launches and manages per-job Docker containers.

Each agent job runs in its own ephemeral Docker container for security
isolation.  The container:
- Runs the shandy-agent image (contains claude-agent-sdk + Node.js)
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
from typing import Any

from shandy.version import SHORT_COMMIT_LENGTH

logger = logging.getLogger(__name__)

AGENT_IMAGE = "shandy-agent:latest"
AGENT_NETWORK = "shandy_default"


class JobContainerRunner:
    """Launches and stops per-job agent containers."""

    def __init__(self) -> None:
        import docker

        self._docker: docker.DockerClient = docker.from_env()

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
        from shandy.settings import get_settings

        settings = get_settings()
        cs = settings.container

        # Translate job_dir from container-internal path to host path.
        # Must resolve to absolute FIRST (so relative paths like "jobs/uuid" become
        # "/app/jobs/uuid" inside the web container), then translate to the host
        # path.  Docker requires absolute paths for bind mounts; relative paths
        # are misinterpreted as named volumes.
        job_dir_host = self._to_host_path(job_dir.resolve(), cs)

        provider_env = settings.provider.get_container_env_vars()
        database_url = settings.database.effective_database_url

        # Mount at a path whose final component IS the job UUID so that
        # orchestrator code can derive the job ID from job_dir.name.
        job_mount = f"/agent/jobs/{job_id}"

        env: dict[str, str] = {
            "JOB_ID": job_id,
            "JOB_DIR": job_mount,
            "DATABASE_URL": database_url,
            "SHANDY_SECRET_KEY": settings.secret_key,
            **provider_env,
        }

        # GCP credentials: mount the file if configured.
        # Also mount the Docker socket so the agent can spawn executor containers.
        volumes: dict[str, dict[str, str]] = {
            str(job_dir_host): {"bind": job_mount, "mode": "rw"},
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
        }
        gcp_path = settings.provider.google_application_credentials
        if gcp_path:
            gcp_host_path = settings.provider.gcp_credentials_host_path or gcp_path
            container_gcp_path = "/agent/gcp-credentials.json"
            volumes[str(gcp_host_path)] = {"bind": container_gcp_path, "mode": "ro"}
            env["GOOGLE_APPLICATION_CREDENTIALS"] = container_gcp_path

        # Pass the docker socket GID so the agent container can use it.
        # We read the GID from the socket itself rather than /etc/group since
        # the docker group may not be present inside the web server container.
        _sock = Path("/var/run/docker.sock")
        docker_gid = str(os.stat(_sock).st_gid) if _sock.exists() else None

        short_id = job_id[:SHORT_COMMIT_LENGTH]
        container = self._docker.containers.run(
            image=AGENT_IMAGE,
            name=f"shandy-agent-{short_id}",
            detach=True,
            remove=False,
            environment=env,
            volumes=volumes,
            network=AGENT_NETWORK,
            mem_limit=cs.agent_memory,
            nano_cpus=int(cs.agent_cpu * 1e9),
            security_opt=["no-new-privileges:true"],
            group_add=[docker_gid] if docker_gid else [],
            labels={
                "shandy.job_id": job_id,
                "shandy.type": "agent",
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
            except Exception as e:
                logger.warning("Failed to stop container for job %s: %s", job_id, e)

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
                    except Exception as log_err:
                        logger.warning(
                            "Failed to save container logs for job %s: %s", job_id, log_err
                        )
                container.remove(force=True)
                logger.info("Removed container for job %s", job_id)
            except Exception as e:
                logger.warning("Failed to remove container for job %s: %s", job_id, e)

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
                return container.attrs["State"]["ExitCode"]
        except Exception as e:
            logger.warning("Failed to get exit code for job %s: %s", job_id, e)
        return None

    def _find_container(self, job_id: str) -> Any | None:
        """Find the running container for a job by label."""
        try:
            containers = self._docker.containers.list(
                all=True,
                filters={"label": f"shandy.job_id={job_id}"},
            )
            return containers[0] if containers else None
        except Exception as e:
            logger.warning("Failed to find container for job %s: %s", job_id, e)
            return None

    @staticmethod
    def _to_host_path(job_dir: Path, cs: Any) -> Path:
        """
        Translate a container-internal path to the host filesystem path.

        When the web server itself runs in Docker, paths like /app/jobs/... need
        to be mapped to their host equivalents for sibling container volume mounts.
        """
        if not cs.host_project_dir:
            return job_dir

        container_app_dir = Path(cs.container_app_dir)
        host_project_dir = Path(cs.host_project_dir)

        try:
            relative = job_dir.relative_to(container_app_dir)
            return host_project_dir / relative
        except ValueError:
            return job_dir

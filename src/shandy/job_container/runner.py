"""
JobContainerRunner — launches and manages per-job Docker containers.

Each agent job runs in its own ephemeral Docker container for security
isolation.  The container:
- Runs the shandy-agent image (contains claude-agent-sdk + Node.js)
- Mounts the job directory as /agent/job
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
from pathlib import Path
from typing import Any

from shandy.version import SHORT_COMMIT_LENGTH

logger = logging.getLogger(__name__)

AGENT_IMAGE = "shandy-agent:latest"
AGENT_NETWORK = "shandy_default"


class JobContainerRunner:
    """Launches and stops per-job agent containers."""

    def __init__(self) -> None:
        try:
            import docker

            self._docker: docker.DockerClient | None = docker.from_env()
            self._available = True
        except Exception as e:
            logger.warning("Docker not available: %s", e)
            self._docker = None
            self._available = False

    def is_available(self) -> bool:
        """Return True if the Docker daemon is reachable."""
        return self._available

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
        if not self._available:
            raise RuntimeError("Docker is not available")

        from shandy.settings import get_settings

        settings = get_settings()
        cs = settings.container

        # Translate job_dir from container-internal path to host path
        job_dir_host = self._to_host_path(job_dir, cs)

        provider_env = settings.provider.get_container_env_vars()
        database_url = settings.database.effective_database_url

        env: dict[str, str] = {
            "JOB_ID": job_id,
            "JOB_DIR": "/agent/job",
            "DATABASE_URL": database_url,
            **provider_env,
        }

        # GCP credentials: mount the file if configured
        volumes: dict[str, dict[str, str]] = {
            str(job_dir_host): {"bind": "/agent/job", "mode": "rw"},
        }
        gcp_path = settings.provider.google_application_credentials
        if gcp_path:
            gcp_host_path = settings.provider.gcp_credentials_host_path or gcp_path
            container_gcp_path = "/agent/gcp-credentials.json"
            volumes[str(gcp_host_path)] = {"bind": container_gcp_path, "mode": "ro"}
            env["GOOGLE_APPLICATION_CREDENTIALS"] = container_gcp_path

        short_id = job_id[:SHORT_COMMIT_LENGTH]
        assert self._docker is not None
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
            labels={
                "shandy.job_id": job_id,
                "shandy.type": "agent",
            },
        )

        logger.info("Launched agent container %s for job %s", container.short_id, job_id)
        return container

    def stop(self, job_id: str, timeout: int = 10) -> None:
        """Stop the container for a job (graceful → SIGKILL)."""
        if not self._available:
            return
        container = self._find_container(job_id)
        if container:
            try:
                container.stop(timeout=timeout)
                logger.info("Stopped container for job %s", job_id)
            except Exception as e:
                logger.warning("Failed to stop container for job %s: %s", job_id, e)

    def cleanup(self, job_id: str) -> None:
        """Remove the container for a job."""
        if not self._available:
            return
        container = self._find_container(job_id)
        if container:
            try:
                container.remove(force=True)
                logger.info("Removed container for job %s", job_id)
            except Exception as e:
                logger.warning("Failed to remove container for job %s: %s", job_id, e)

    def _find_container(self, job_id: str) -> Any | None:
        """Find the running container for a job by label."""
        if self._docker is None:
            return None
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

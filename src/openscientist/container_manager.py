"""
Container manager for OpenScientist sibling container isolation.

Manages the lifecycle of executor containers for isolated code execution.
Each job's code runs in a separate Docker container with strict resource
limits, started by the container launcher, the only process that holds the
Docker socket.
"""

import json
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openscientist.admin_settings import current_admin_settings_sync
from openscientist.exceptions import MissingImageError
from openscientist.job_container import to_host_path
from openscientist.job_container.identity import apply_job_ownership
from openscientist.job_container.launcher_client import (
    LauncherBadRequestError,
    LauncherClient,
    LauncherDockerError,
    LauncherError,
    LauncherImageMissingError,
    LauncherUnavailableError,
)
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def _container_error_result(
    *,
    error: str,
    output: str = "",
    execution_time: float = 0.0,
) -> dict[str, Any]:
    return {
        "success": False,
        "error": error,
        "output": output,
        "plots": [],
        "execution_time": execution_time,
    }


class ContainerManager:
    """
    Manages executor containers for isolated code execution.

    Features:
    - Spawn executor containers through the container launcher
    - Pass code via the launcher's exec endpoint, collect results via stdout
    - The launcher removes every container after the run
    - Support for job-based container cleanup and orphan reaping
    """

    def __init__(
        self,
        image: str | None = None,
        memory_mib: int | None = None,
        cpu_limit: float | None = None,
        timeout: int | None = None,
    ):
        """
        Initialize container manager.

        The memory, CPU, and timeout limits are read from the admin settings
        row at each execution when not given here, so an admin edit applies at
        the next execution.

        Args:
            image: Docker image for executor containers, named in error messages
            memory_mib: Memory limit in MiB
            cpu_limit: CPU limit as a fraction (e.g., 0.5 = 50% of one CPU)
            timeout: Default execution timeout in seconds
        """
        settings = get_settings()
        self.image = image or settings.container.executor_image
        self.memory_mib = memory_mib
        self.cpu_limit = cpu_limit
        self.timeout = timeout
        self._client: LauncherClient | None = None

    @property
    def client(self) -> LauncherClient:
        """Lazy-load the launcher client."""
        if self._client is None:
            self._client = LauncherClient()
        return self._client

    def _parse_executor_result(self, result: bytes | str) -> dict[str, Any]:
        if isinstance(result, bytes):
            result = result.decode("utf-8")
        try:
            parsed: dict[str, Any] = json.loads(result)
            return parsed
        except json.JSONDecodeError as e:
            return _container_error_result(
                error=f"Failed to parse executor output: {e}",
                output=result[:1000],
            )

    def execute_code(
        self,
        code: str,
        job_id: str,
        data_path: str | None = None,
        output_dir: str | Path | None = None,
        timeout: int | None = None,
        description: str = "",
        iteration: int = 0,
        data_files: list[dict[str, Any]] | None = None,
        language: str = "python",
    ) -> dict[str, Any]:
        """
        Execute code in an isolated container.

        The launcher builds the container from its executor template, mounts
        the output directory and the data files' parent directories, and
        rewrites the payload's paths to container paths. It therefore only
        accepts paths under the job's own directory.

        Args:
            code: Code to execute
            job_id: Job identifier for container labeling and cleanup
            data_path: Optional path to the primary data file
            output_dir: Directory to save plots (must be under the job directory)
            timeout: Execution timeout in seconds (default: class default)
            description: Description of what's being investigated
            iteration: Current iteration number
            data_files: List of file metadata dicts, each with a "path"
            language: Programming language ("python" or "rust")

        Returns:
            Execution result dictionary with success, output, plots, error, etc.
        """
        admin = current_admin_settings_sync()
        timeout = timeout if timeout is not None else self.timeout
        if timeout is None:
            timeout = admin.executor_timeout_seconds
        memory_mib = self.memory_mib if self.memory_mib is not None else admin.executor_memory_mib
        cpu_limit = self.cpu_limit if self.cpu_limit is not None else admin.executor_cpu

        cs = get_settings().container
        job_root = Path(cs.container_app_dir) / "jobs" / job_id
        if output_dir is None:
            # The launcher only mounts output directories under the job directory.
            job_root.mkdir(parents=True, exist_ok=True)
            output_dir = Path(tempfile.mkdtemp(prefix="exec-out-", dir=job_root))
        else:
            output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        # The executor container writes here as its own uid, sharing the job group.
        apply_job_ownership(output_dir)

        def _host(path_str: str) -> str:
            # The launcher runs on the host, so the web-container paths become host paths.
            return str(to_host_path(Path(path_str).resolve(), cs))

        logger.info(
            "Spawning executor container for job %s (image=%s, mem=%dMiB, cpu=%.2f)",
            job_id,
            self.image,
            memory_mib,
            cpu_limit,
        )

        # The launcher's wall-clock bound must clear the executor's own
        # timeout alarm, which is what turns a slow run into a timeout result.
        request: dict[str, Any] = {
            "job_id": job_id,
            "timeout_seconds": timeout + 30,
            "memory_mib": memory_mib,
            "cpu": cpu_limit,
            "airgapped": admin.airgapped,
            "input": {
                "code": code,
                "language": language,
                "data_path": _host(data_path) if data_path else None,
                "output_dir": _host(str(output_dir)),
                "timeout": timeout,
                "description": description,
                "iteration": iteration,
                "data_files": [
                    {**f, "path": _host(f["path"])} if f.get("path") else dict(f)
                    for f in data_files or []
                ],
            },
        }

        try:
            # The launcher removes its container on every path.
            response = self.client.run_exec(request)
        except LauncherImageMissingError:
            logger.error("Executor image not found: %s", self.image)
            return _container_error_result(error=str(MissingImageError(self.image)))
        except LauncherBadRequestError as e:
            logger.error("The launcher refused the executor request: %s", e)
            return _container_error_result(error=f"Executor request refused: {e}")
        except LauncherDockerError as e:
            logger.error("Docker API error from the launcher: %s", e)
            return _container_error_result(error=f"Docker API error: {e}")
        except LauncherUnavailableError as e:
            logger.error("The container launcher is not reachable: %s", e)
            return _container_error_result(error=f"Container launcher unavailable: {e}")
        except Exception as e:
            logger.error("Unexpected error in container execution: %s", e)
            return _container_error_result(
                error=f"Container execution error: {type(e).__name__}: {e}",
            )

        execution_result = self._parse_executor_result(response["stdout"])

        logger.info(
            "Executor run for job %s completed: success=%s, time=%.2fs",
            job_id,
            execution_result.get("success"),
            execution_result.get("execution_time", 0),
        )

        return execution_result

    def cleanup_job_containers(self, job_id: str) -> int:
        """
        Remove all launcher-created containers associated with a job.

        Args:
            job_id: Job identifier

        Returns:
            Number of containers removed
        """
        removed = 0
        try:
            entries = self.client.list_containers([f"openscientist.job_id={job_id}"])
        except LauncherError as e:
            logger.error("Failed to list containers for job %s: %s", job_id, e)
            return removed

        for entry in entries:
            try:
                self.client.remove(entry["id"])
                removed += 1
                logger.info("Removed container %s for job %s", entry["name"], job_id)
            except LauncherError as e:
                logger.warning("Failed to remove container %s: %s", entry["name"], e)

        return removed

    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int:
        """
        Remove stale executor containers older than max_age.

        Args:
            max_age_hours: Maximum age in hours before a container is orphaned

        Returns:
            Number of containers removed
        """
        removed = 0
        try:
            entries = self.client.list_containers(["openscientist.type=executor"])
        except LauncherError as e:
            logger.error("Failed to list orphaned containers: %s", e)
            return removed

        now = datetime.now(UTC)

        for entry in entries:
            created_str = entry.get("created") or ""
            if not created_str:
                continue
            try:
                # The launcher echoes the daemon's ISO timestamp.
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_hours = (now - created).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            if age_hours <= max_age_hours:
                continue
            try:
                self.client.remove(entry["id"])
                removed += 1
                logger.info(
                    "Removed orphaned container %s (age: %.1f hours)",
                    entry["name"],
                    age_hours,
                )
            except LauncherError as e:
                logger.warning(
                    "Failed to remove orphaned container %s: %s",
                    entry["name"],
                    e,
                )

        return removed

    def check_image_available(self, image: str | None = None) -> bool:
        """
        Check whether one of the launcher's template images is on the Docker host.

        Args:
            image: The image to look for, the executor image when omitted

        Returns:
            True if the launcher reports the image present
        """
        # The launcher only knows its two template images, which map to its
        # health booleans; every image the web checks is one of the two.
        name = "executor" if (image or self.image) == self.image else "agent"
        try:
            health = self.client.health()
        except LauncherError:
            return False
        return bool(health["images"].get(name))

    def is_available(self) -> bool:
        """
        Check if the launcher answers and its Docker daemon is up.

        Returns:
            True if the launcher is reachable and working
        """
        try:
            return bool(self.client.health().get("docker"))
        except LauncherError as exc:
            logger.debug("Launcher health check failed: %s", exc)
            return False


# Global instance for convenience
_container_manager: ContainerManager | None = None


def get_container_manager() -> ContainerManager:
    """Get the global container manager instance."""
    global _container_manager
    if _container_manager is None:
        _container_manager = ContainerManager()
    return _container_manager

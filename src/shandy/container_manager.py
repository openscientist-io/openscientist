"""
Container manager for SHANDY sibling container isolation.

Manages the lifecycle of executor containers for isolated code execution.
Each job's code runs in a separate Docker container with strict resource limits.
"""

import base64
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shandy.settings import get_settings

if TYPE_CHECKING:
    import docker as _docker_module

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
    Manages Docker containers for isolated code execution.

    Features:
    - Spawn containers with strict resource limits
    - Pass code via stdin, collect results via stdout
    - Automatic cleanup of containers after execution
    - Support for job-based container cleanup
    """

    def __init__(
        self,
        image: str | None = None,
        memory_limit: str | None = None,
        cpu_limit: float | None = None,
        timeout: int | None = None,
    ):
        """
        Initialize container manager.

        Args:
            image: Docker image for executor containers
            memory_limit: Memory limit (e.g., "2g")
            cpu_limit: CPU limit as fraction (e.g., 0.5 = 50% of one CPU)
            timeout: Default execution timeout in seconds
        """
        settings = get_settings()
        self.image = image or settings.container.executor_image
        self.memory_limit = memory_limit or settings.container.executor_memory
        self.cpu_limit = cpu_limit if cpu_limit is not None else settings.container.executor_cpu
        self.timeout = timeout if timeout is not None else settings.container.executor_timeout
        self._client: "_docker_module.DockerClient | None" = None

    @property
    def client(self) -> "_docker_module.DockerClient":
        """Lazy-load Docker client."""
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def _encode_executor_input(
        self,
        *,
        code: str,
        language: str,
        data_path: str | None,
        timeout: int,
        description: str,
        iteration: int,
        data_files: list[dict[str, Any]] | None,
    ) -> str:
        input_data = {
            "code": code,
            "language": language,
            "data_path": data_path,
            "output_dir": "/output",
            "timeout": timeout,
            "description": description,
            "iteration": iteration,
            "data_files": data_files or [],
        }
        input_json = json.dumps(input_data)
        return base64.b64encode(input_json.encode()).decode()

    def _build_volumes(
        self,
        *,
        output_dir: Path,
        data_files: list[dict[str, Any]] | None,
    ) -> dict[str, dict[str, str]]:
        volumes: dict[str, dict[str, str]] = {
            str(output_dir): {"bind": "/output", "mode": "rw"},
        }
        if not data_files:
            return volumes

        for file_info in data_files:
            file_path = Path(file_info.get("path", "")).resolve()
            if not file_path.exists():
                continue
            volumes[str(file_path.parent)] = {"bind": "/data", "mode": "ro"}
            break

        return volumes

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

    def _cleanup_container_by_name(self, container_name: str) -> None:
        import docker.errors

        try:
            container = self.client.containers.get(container_name)
            container.remove(force=True)
        except docker.errors.NotFound:
            return

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

        Args:
            code: Code to execute
            job_id: Job identifier for container labeling and cleanup
            data_path: Optional path to data file (inside container)
            output_dir: Directory to save plots (will be mounted)
            timeout: Execution timeout in seconds (default: class default)
            description: Description of what's being investigated
            iteration: Current iteration number
            data_files: List of file metadata dicts
            language: Programming language ("python" or "rust")

        Returns:
            Execution result dictionary with success, output, plots, error, etc.
        """
        import docker.errors

        timeout = timeout or self.timeout
        output_dir = Path(output_dir).resolve() if output_dir else Path(tempfile.mkdtemp())
        output_dir.mkdir(parents=True, exist_ok=True)

        input_b64 = self._encode_executor_input(
            code=code,
            language=language,
            data_path=data_path,
            timeout=timeout,
            description=description,
            iteration=iteration,
            data_files=data_files,
        )

        # Container name includes job_id for cleanup
        container_name = f"shandy-exec-{job_id}-{os.urandom(4).hex()}"

        volumes = self._build_volumes(output_dir=output_dir, data_files=data_files)

        logger.info(
            "Spawning executor container: %s (image=%s, mem=%s, cpu=%.2f)",
            container_name,
            self.image,
            self.memory_limit,
            self.cpu_limit,
        )

        try:
            # Run container
            result = self.client.containers.run(
                image=self.image,
                name=container_name,
                stdin_open=True,
                remove=False,  # We remove manually after getting logs
                detach=False,
                # Resource limits
                mem_limit=self.memory_limit,
                nano_cpus=int(self.cpu_limit * 1e9),
                # Security settings
                network_disabled=True,
                read_only=False,  # Need write access for plots
                security_opt=["no-new-privileges:true"],
                user="executor",
                # Labels for cleanup
                labels={
                    "shandy.job_id": job_id,
                    "shandy.type": "executor",
                },
                # Volume mounts
                volumes=volumes,
                # Timeout
                stop_signal="SIGKILL",
                # Clear the entrypoint so we can use shell piping
                entrypoint=[],
                # Input via base64-encoded stdin to avoid shell quoting issues
                command=[
                    "sh",
                    "-c",
                    f"echo {input_b64} | base64 -d | python -m shandy_executor",
                ],
            )

            execution_result = self._parse_executor_result(result)
            self._cleanup_container_by_name(container_name)

            logger.info(
                "Executor container %s completed: success=%s, time=%.2fs",
                container_name,
                execution_result.get("success"),
                execution_result.get("execution_time", 0),
            )

            return execution_result

        except docker.errors.ContainerError as e:
            logger.error("Container %s failed with error: %s", container_name, e)

            # Try to get logs
            try:
                container = self.client.containers.get(container_name)
                logs = container.logs().decode("utf-8")
                container.remove(force=True)
            except docker.errors.NotFound:
                logs = str(e)

            return _container_error_result(
                error=f"Container execution failed: {e}",
                output=logs[:2000],
            )

        except docker.errors.ImageNotFound:
            logger.error("Executor image not found: %s", self.image)
            return _container_error_result(
                error=(
                    f"Executor image not found: {self.image}. "
                    "Run 'make build-executor' to build it."
                ),
            )

        except docker.errors.APIError as e:
            logger.error("Docker API error: %s", e)
            return _container_error_result(error=f"Docker API error: {e}")

        except Exception as e:
            logger.error("Unexpected error in container execution: %s", e)
            return _container_error_result(
                error=f"Container execution error: {type(e).__name__}: {e}",
            )

    def cleanup_job_containers(self, job_id: str) -> int:
        """
        Remove all containers associated with a job.

        Args:
            job_id: Job identifier

        Returns:
            Number of containers removed
        """
        import docker.errors

        removed = 0
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": f"shandy.job_id={job_id}"},
            )

            for container in containers:
                try:
                    container.remove(force=True)
                    removed += 1
                    logger.info("Removed container %s for job %s", container.name, job_id)
                except docker.errors.APIError as e:
                    logger.warning("Failed to remove container %s: %s", container.name, e)

        except docker.errors.APIError as e:
            logger.error("Failed to list containers for job %s: %s", job_id, e)

        return removed

    def cleanup_orphaned_containers(self, max_age_hours: int = 24) -> int:
        """
        Remove stale executor containers older than max_age.

        Args:
            max_age_hours: Maximum age in hours before container is considered orphaned

        Returns:
            Number of containers removed
        """
        import docker.errors

        removed = 0
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": "shandy.type=executor"},
            )

            now = datetime.now(UTC)

            for container in containers:
                try:
                    # Parse container creation time
                    created_str = container.attrs.get("Created", "")
                    if created_str:
                        # Docker returns ISO format with microseconds
                        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        age_hours = (now - created).total_seconds() / 3600

                        if age_hours > max_age_hours:
                            container.remove(force=True)
                            removed += 1
                            logger.info(
                                "Removed orphaned container %s (age: %.1f hours)",
                                container.name,
                                age_hours,
                            )
                except docker.errors.APIError as e:
                    logger.warning(
                        "Failed to remove orphaned container %s: %s",
                        container.name,
                        e,
                    )

        except docker.errors.APIError as e:
            logger.error("Failed to list orphaned containers: %s", e)

        return removed

    def check_image_available(self) -> bool:
        """
        Check if the executor image is available.

        Returns:
            True if image exists, False otherwise
        """
        import docker.errors

        try:
            self.client.images.get(self.image)
            return True
        except docker.errors.ImageNotFound:
            return False
        except docker.errors.APIError:
            return False

    def is_available(self) -> bool:
        """
        Check if Docker is available and working.

        Returns:
            True if Docker is accessible, False otherwise
        """
        try:
            self.client.ping()
            return True
        except Exception as exc:
            logger.debug("Docker ping failed: %s", exc)
            return False


# Global instance for convenience
_container_manager: ContainerManager | None = None


def get_container_manager() -> ContainerManager:
    """Get the global container manager instance."""
    global _container_manager
    if _container_manager is None:
        _container_manager = ContainerManager()
    return _container_manager

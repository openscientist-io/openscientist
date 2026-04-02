"""
Job container management for OpenScientist.

Each agent job runs in its own Docker container:
- runner.py — JobContainerRunner: launch/stop/cleanup containers
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from openscientist.job_container.runner import JobContainerRunner

if TYPE_CHECKING:
    import docker as _docker_module

logger = logging.getLogger(__name__)

__all__ = ["JobContainerRunner", "resolve_docker_network", "to_host_path"]


class _HostPathSettings(Protocol):
    """Minimal settings interface required by to_host_path()."""

    host_project_dir: str | None
    container_app_dir: str


def to_host_path(path: Path, cs: _HostPathSettings) -> Path:
    """Translate a container-internal path to the host filesystem path.

    When the web server itself runs in Docker, paths like /app/jobs/... need
    to be mapped to their host equivalents for sibling container volume mounts.

    Args:
        path: The path to translate (may be container-internal or host).
        cs: Settings object with host_project_dir and container_app_dir.

    Returns:
        The translated host path, or the original path if no translation is needed.
    """
    if not cs.host_project_dir:
        return path

    container_app_dir = Path(cs.container_app_dir)
    host_project_dir = Path(cs.host_project_dir)

    try:
        relative = path.relative_to(container_app_dir)
        return host_project_dir / relative
    except ValueError:
        return path


def resolve_docker_network(
    client: "_docker_module.DockerClient",
    configured_network: str | None,
) -> str:
    """Resolve the Docker network for sibling containers.

    Priority: explicit setting → auto-detect from web container → fallback.
    """
    if configured_network:
        return configured_network

    try:
        hostname = Path("/etc/hostname").read_text().strip()
        container = client.containers.get(hostname)
        networks_raw: Any = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        if isinstance(networks_raw, dict):
            for name in networks_raw:
                if isinstance(name, str) and name != "bridge":
                    return name
    except Exception as e:
        logger.warning("Failed to auto-detect Docker network: %s", e)

    return "bridge"

"""
Job container management for OpenScientist.

Each agent job runs in its own Docker container:
- runner.py — JobContainerRunner: launch/stop/cleanup containers
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openscientist.job_container.runner import JobContainerRunner

if TYPE_CHECKING:
    import docker as _docker_module

logger = logging.getLogger(__name__)

__all__ = ["JobContainerRunner", "resolve_docker_network"]


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

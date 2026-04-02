"""
Helpers for job container path translation and Docker network resolution.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from docker import errors as docker_errors

if TYPE_CHECKING:
    import docker as _docker_module

logger = logging.getLogger(__name__)


class HostPathSettings(Protocol):
    # pylint: disable=too-few-public-methods
    """Minimal settings interface required by ``to_host_path()``."""

    host_project_dir: str | None
    container_app_dir: str


def to_host_path(path: Path, cs: HostPathSettings) -> Path:
    """Translate a container-internal path to the host filesystem path."""
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
    """Resolve the Docker network for sibling containers."""
    if configured_network:
        return configured_network

    try:
        hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        container = client.containers.get(hostname)
        networks_raw: Any = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        if isinstance(networks_raw, dict):
            for name in networks_raw:
                if isinstance(name, str) and name != "bridge":
                    return name
    except (docker_errors.DockerException, OSError) as error:  # pragma: no cover
        logger.warning("Failed to auto-detect Docker network: %s", error)

    return "bridge"

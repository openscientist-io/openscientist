"""
Helpers for job container path translation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


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


def to_container_path(path: Path, cs: HostPathSettings) -> Path:
    """Inverse of to_host_path. A path outside host_project_dir is returned unchanged."""
    if not cs.host_project_dir:
        return path

    try:
        relative = path.relative_to(Path(cs.host_project_dir))
        return Path(cs.container_app_dir) / relative
    except ValueError:
        return path

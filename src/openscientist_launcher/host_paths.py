"""The jobs root must be a host path, because Docker resolves bind sources on the host."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

import docker
from docker import errors as docker_errors
from openscientist_launcher.config import LauncherConfig


def jobs_root_problem(cfg: LauncherConfig, mounts: list[dict[str, Any]] | None) -> str | None:
    """Why ``cfg.jobs_root`` cannot serve as the bind source of job mounts, or None.

    ``mounts`` is the launcher container's own mount list, or None when the
    launcher runs on the host, where every path it sees is a host path.
    """
    root = cfg.jobs_root
    if not root.is_absolute():
        return f"JOBS_ROOT {root} must be an absolute path"
    if not root.is_dir():
        return f"JOBS_ROOT {root} is not a directory"
    if mounts is None:
        return None
    target = PurePosixPath(os.path.normpath(root))
    covering = [
        mount
        for mount in mounts
        if target.is_relative_to(PurePosixPath(str(mount.get("Destination", ""))))
    ]
    if not covering:
        return f"JOBS_ROOT {root} is not mounted from the host"
    mount = max(covering, key=lambda m: len(str(m["Destination"])))
    if mount.get("Type") != "bind":
        return f"JOBS_ROOT {root} is on a {mount.get('Type')} mount, not a bind mount from the host"
    source = PurePosixPath(os.path.normpath(str(mount["Source"])))
    host_path = source / target.relative_to(PurePosixPath(str(mount["Destination"])))
    if host_path != target:
        return (
            f"JOBS_ROOT {root} is mounted from {host_path} on the host. Docker resolves "
            "bind sources on the host, so mount the jobs directory at its host path and "
            "set JOBS_ROOT to that path"
        )
    return None


def check_jobs_root(cfg: LauncherConfig, client: docker.DockerClient) -> str | None:
    """``jobs_root_problem`` for this process. Its own container, named by
    /etc/hostname as the network lookup finds it, supplies the mounts; no such
    container means the launcher runs on the host."""
    mounts = None
    try:
        hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        mounts = client.containers.get(hostname).attrs.get("Mounts", [])
    except docker_errors.NotFound:
        pass
    except (docker_errors.DockerException, OSError) as exc:
        return f"the launcher could not read its own mounts to check JOBS_ROOT: {exc}"
    return jobs_root_problem(cfg, mounts)

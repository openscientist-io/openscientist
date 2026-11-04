"""Compose health check: the socket as the web reaches it, the daemon and both template images."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import httpx
import requests

import docker
from docker import errors as docker_errors
from openscientist_launcher.config import ConfigError, LauncherConfig
from openscientist_launcher.images import image_available

_AS_WEB_FLAG = "--as-web"


def _request_health(cfg: LauncherConfig) -> int:
    """GET /v1/health through the socket, from the child that holds the web's ids."""
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(cfg.socket_path)), base_url="http://launcher"
        ) as client:
            response = client.get("/v1/health", timeout=5)
    except httpx.HTTPError as exc:
        print(exc, file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"HTTP {response.status_code}", file=sys.stderr)
        return 1
    return 0


def _web_socket_failure(cfg: LauncherConfig) -> str | None:
    """Why a process with the web's uid and gid cannot use the socket, or None.

    The check runs as root, which may open any file, so the request comes from
    a child process switched to the web's identity.
    """
    identity: dict[str, Any] = {}
    if (os.geteuid(), os.getegid()) != (cfg.web_uid, cfg.web_gid):
        identity = {"user": cfg.web_uid, "group": cfg.web_gid, "extra_groups": []}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "openscientist_launcher.health", _AS_WEB_FLAG],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            **identity,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the socket could not be checked as the web: {exc}"
    if result.returncode != 0:
        reason = result.stderr.strip().splitlines()[-1:] or ["no reason given"]
        return (
            f"uid {cfg.web_uid} gid {cfg.web_gid} cannot use the socket at "
            f"{cfg.socket_path}: {reason[0]}"
        )
    return None


def main() -> int:
    try:
        cfg = LauncherConfig.from_env(os.environ)
    except ConfigError as exc:
        print(f"launcher health: {exc}", file=sys.stderr)
        return 1
    if sys.argv[1:] == [_AS_WEB_FLAG]:
        return _request_health(cfg)

    failures: list[str] = []
    socket_failure = _web_socket_failure(cfg)
    if socket_failure is not None:
        failures.append(socket_failure)

    client = docker.from_env()
    try:
        client.ping()
    except (docker_errors.DockerException, requests.exceptions.RequestException) as exc:
        failures.append(f"docker daemon not answering: {exc}")
        docker_up = False
    else:
        docker_up = True

    if docker_up:
        for name, image, digests in (
            ("agent", cfg.agent_image, cfg.agent_image_digests),
            ("executor", cfg.executor_image, cfg.executor_image_digests),
        ):
            try:
                present = image_available(client, image, digests)
            except (docker_errors.DockerException, requests.exceptions.RequestException):
                present = False
            if not present:
                failures.append(f"{name} image is missing or off the digest allow-list")

    if failures:
        print("launcher health: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

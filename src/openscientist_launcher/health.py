"""Compose health check: the socket file, the daemon and both template images."""

from __future__ import annotations

import os
import sys

import requests

import docker
from docker import errors as docker_errors
from openscientist_launcher.config import ConfigError, LauncherConfig
from openscientist_launcher.images import image_available


def main() -> int:
    try:
        cfg = LauncherConfig.from_env(os.environ)
    except ConfigError as exc:
        print(f"launcher health: {exc}", file=sys.stderr)
        return 1

    failures: list[str] = []
    if not cfg.socket_path.exists():
        failures.append(f"socket file missing at {cfg.socket_path}")

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

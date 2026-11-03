"""Image presence and digest allow-list checks."""

from __future__ import annotations

import docker
from docker import errors as docker_errors


def image_available(client: docker.DockerClient, image: str, digests: frozenset[str]) -> bool:
    """Present in the daemon and on the digest allow-list the CLI recorded."""
    try:
        image_obj = client.images.get(image)
    except docker_errors.ImageNotFound:
        return False
    return image_obj.attrs.get("Id", "") in digests

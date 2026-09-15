"""R6 regression guards on docker-compose.yml.

Static (no-Docker) checks that fail loudly if anyone re-adds a raw
/var/run/docker.sock mount to the app or agent, drops the docker-socket-proxy,
or loosens its Docker-API whitelist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"
_SOCKET = "/var/run/docker.sock"
_PROXY = "docker-socket-proxy"
_APP = "openscientist"

# The exact API surface the container-per-job lifecycle needs, and nothing else.
_ALLOWED = (
    "CONTAINERS",
    "IMAGES",
    "POST",
    "DELETE",
    "PING",
    "VERSION",
)
_DENIED = (
    "INFO",
    "EXEC",
    "NETWORKS",
    "VOLUMES",
    "BUILD",
    "COMMIT",
    "AUTH",
    "SYSTEM",
    "SWARM",
    "SECRETS",
    "CONFIGS",
    "SERVICES",
    "TASKS",
    "NODES",
    "PLUGINS",
    "DISTRIBUTION",
    "SESSION",
    "EVENTS",
)


def _compose() -> Any:
    return yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))


def _socket_mounts(service: Any) -> list[str]:
    volumes = service.get("volumes") or []
    return [v for v in volumes if isinstance(v, str) and _SOCKET in v]


def test_only_the_proxy_mounts_the_docker_socket() -> None:
    """No service except docker-socket-proxy may bind-mount the host socket."""
    services = _compose()["services"]
    mounters = {name for name, svc in services.items() if _socket_mounts(svc)}
    assert mounters == {_PROXY}, f"unexpected socket mounters: {sorted(mounters - {_PROXY})}"


def test_proxy_mounts_socket_read_only() -> None:
    """The proxy's socket mount must be read-only."""
    mounts = _socket_mounts(_compose()["services"][_PROXY])
    assert mounts, "proxy must mount the docker socket"
    assert all(m.rstrip().endswith(":ro") for m in mounts), mounts


def test_app_reaches_docker_only_via_proxy() -> None:
    """The web app has no socket mount / docker group and talks to the proxy."""
    app = _compose()["services"][_APP]
    assert app["environment"]["DOCKER_HOST"] == f"tcp://{_PROXY}:2375"
    assert not _socket_mounts(app)
    assert "group_add" not in app


def test_proxy_whitelist_is_least_privilege() -> None:
    """The proxy enables exactly the required verbs and denies everything else."""
    env = _compose()["services"][_PROXY]["environment"]
    for key in _ALLOWED:
        assert str(env.get(key)) == "1", f"{key} must be enabled (=1)"
    for key in _DENIED:
        assert str(env.get(key)) == "0", f"{key} must be denied (=0)"

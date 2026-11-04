"""The web container's built posture, read from the files a build actually uses.

The production compose file, the dev override and the web Dockerfile are
parsed and merged the way Docker would, so the checks follow what the built
web container and its runtime configuration are. The launcher is the only
service allowed to hold the Docker socket, the web runs as its own non-root
user in the jobs group, and the web's code holds no Docker client.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE_DIR = REPO_ROOT / "src" / "openscientist"
WEB_DOCKERFILE = REPO_ROOT / "Dockerfile"
PRODUCTION_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_COMPOSE = REPO_ROOT / "docker-compose.dev.yml"

DOCKER_SOCKET = "/var/run/docker.sock"


def _compose_services(path: Path) -> dict[str, dict[str, object]]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path.name} must be a compose file"
    services = loaded["services"]
    assert isinstance(services, dict)
    return services


def _merged_services() -> dict[str, dict[str, object]]:
    """The dev view: the production file overridden by the dev file."""
    merged = {
        name: dict(service) for name, service in _compose_services(PRODUCTION_COMPOSE).items()
    }
    for name, extra in _compose_services(DEV_COMPOSE).items():
        service = merged.setdefault(name, {})
        for key, value in extra.items():
            if key == "volumes":
                assert isinstance(value, list)
                existing = service.get(key)
                base = list(existing) if isinstance(existing, list) else []
                service[key] = [*base, *value]
            else:
                service[key] = value
    return merged


def _service_volumes(service: dict[str, object]) -> list[str]:
    volumes = service.get("volumes")
    if volumes is None:
        return []
    assert isinstance(volumes, list)
    return [str(volume) for volume in volumes]


def _socket_mounts(service: dict[str, object]) -> list[str]:
    return [volume for volume in _service_volumes(service) if DOCKER_SOCKET in volume.split(":")[0]]


def test_production_web_service_holds_no_docker_socket() -> None:
    """The production web loses the socket and the docker group; only the
    launcher keeps a socket mount, plain, as the trusted holder."""
    services = _compose_services(PRODUCTION_COMPOSE)
    web = services["openscientist"]
    assert _socket_mounts(web) == []
    assert web.get("group_add") in (None, []), "the web no longer joins the docker group"
    assert _socket_mounts(services["migrate"]) == []
    launcher = services["launcher"]
    assert _socket_mounts(launcher), "the launcher is the socket holder"
    assert all(":" not in volume.split(":")[1] for volume in _socket_mounts(launcher)), (
        "the launcher's socket mount is not read-only"
    )


def _web_identity_from_dockerfile() -> tuple[str, int, int]:
    """(user, uid, gid) of the web image's USER directive and its useradd."""
    lines = WEB_DOCKERFILE.read_text(encoding="utf-8").splitlines()
    user = None
    uid: int | None = None
    gid: int | None = None
    for line in lines:
        stripped = line.strip()
        if match := re.match(r"^USER\s+(\S+)", stripped):
            user = match.group(1)
        if (uid_match := re.search(r"useradd[^&]*--uid\s+(\d+)", stripped)) and uid is None:
            uid = int(uid_match.group(1))
        if (gid_match := re.search(r"useradd[^&]*--gid\s+(\d+)", stripped)) and gid is None:
            gid = int(gid_match.group(1))
    assert user and uid is not None and gid is not None, (
        "the web Dockerfile must define its user with an explicit uid and gid"
    )
    return user, uid, gid


def test_web_runs_as_its_own_non_root_user_in_the_jobs_group() -> None:
    """The web image switches to a non-root user of the jobs group, and the
    launcher's configured web identity is that same uid and gid."""
    user, uid, gid = _web_identity_from_dockerfile()
    assert user != "root"
    assert uid != 0, "the web user must not be root"
    assert gid == 1001, "the web user's group is the shared jobs group"

    launcher_env = _compose_services(PRODUCTION_COMPOSE)["launcher"]["environment"]
    assert isinstance(launcher_env, dict)
    assert str(launcher_env["LAUNCHER_WEB_UID"]) == str(uid)
    assert str(launcher_env["LAUNCHER_WEB_GID"]) == str(gid)


def test_dev_override_restores_only_the_source_mount() -> None:
    """Dev mode gives the web and migrate back ./src and nothing else: the
    socket and the docker group stay gone in every view."""
    production = _compose_services(PRODUCTION_COMPOSE)
    for name in ("openscientist", "migrate"):
        assert not any("/app/src" in volume for volume in _service_volumes(production[name])), (
            f"{name} keeps a source mount out of dev mode"
        )

    merged = _merged_services()
    for name in ("openscientist", "migrate"):
        assert any(
            volume.startswith("./src:/app/src") and ":ro" not in volume
            for volume in _service_volumes(merged[name])
        ), f"{name} does not get the source mount back in dev mode"
    for name, service in merged.items():
        if name == "launcher":
            continue
        assert _socket_mounts(service) == [], f"{name} regains the socket in dev mode"
        assert service.get("group_add") in (None, []), f"{name} regains the docker group"


def _module_imports(module: ast.Module) -> Iterator[ast.alias]:
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            yield from node.names
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield ast.alias(name=node.module, col_offset=node.col_offset)


def test_no_web_module_imports_the_docker_sdk() -> None:
    """The web package reaches Docker only through the launcher client: no
    module in it imports the docker SDK, so no web code path can hold the
    socket."""
    offenders: list[str] = []
    for path in sorted(WEB_SOURCE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for alias in _module_imports(tree):
            if alias.name.split(".")[0] == "docker":
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {alias.name}")
    assert offenders == []

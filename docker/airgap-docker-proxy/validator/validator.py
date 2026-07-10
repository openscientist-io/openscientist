"""Validator sidecar for the airgap Docker socket proxy (RFC section 9 /
issue #218).

Sits in front of the haproxy service (``../haproxy/``), which is only
reachable from this container -- see ``docker-compose.yml``'s
``airgap-proxy-backend`` network. The agent container only ever talks to
this validator, never directly to haproxy or the real Docker socket.

Two jobs:

1. ``POST /containers/create``: parse the JSON body and reject (403)
   anything that could escape the sandbox or defeat the airgap network
   boundary -- see :func:`_reject_reason`. On acceptance, forward the
   (unmodified) body to the backend.
2. Everything else: transparent passthrough to the backend, which does its
   own independent path+method allowlist (``../haproxy/haproxy.cfg``).

Both layers must independently agree on what counts as each route. This
file's path normalization (stripping a leading ``/v<major>.<minor>``
version prefix) is deliberately written the same way as
``haproxy.cfg``'s, so a request crafted to fool one layer's matching
doesn't silently reach the other with a different interpretation of the
same URL.

Explicitly hard-denies any method on ``/containers/{id}/exec`` even though
``haproxy.cfg`` already denies it by omission (no allow rule matches it).
Redundant on purpose: this is the layer that actually parses each
request's semantics, so a single mistake in the other layer's static regex
list isn't enough to reopen that gap.

Docker's Python SDK's ``containers.run(detach=False, ...)`` (the only
call path that reaches ``POST /containers/create`` in this codebase --
see ``container_manager.py``) is a plain create -> start -> wait -> logs
sequence, all ordinary request/response calls with no streaming
``stream=True`` argument, no ``/containers/{id}/attach`` "hijacked"
connection upgrade. This validator only needs to proxy plain HTTP
request/response pairs, not raw stream upgrades.
"""

from __future__ import annotations

import json
import logging
import os
import re

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger("airgap_docker_proxy_validator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_BACKEND_BASE_URL = os.environ.get(
    "AIRGAP_DOCKER_PROXY_BACKEND", "http://airgap-docker-proxy-haproxy:2375"
)

# Comma-separated allowlist of image names/tags the executor is permitted
# to run. Set via docker-compose.yml from the same OPENSCIENTIST_EXECUTOR_IMAGE
# value the application itself uses -- a compromised or manipulated agent
# can't ask to run some other locally-cached image instead of the intended
# executor sandbox.
_ALLOWED_IMAGES = frozenset(
    img.strip()
    for img in os.environ.get("AIRGAP_DOCKER_PROXY_ALLOWED_IMAGES", "").split(",")
    if img.strip()
)

_VERSION_PREFIX_RE = re.compile(r"^/v[0-9]+\.[0-9]+")
_EXEC_PATH_RE = re.compile(r"^/containers/[a-zA-Z0-9_.-]+/exec(/.*)?$", re.IGNORECASE)


def _normalized_path(raw_path: str) -> str:
    """Strip a leading Docker API version prefix, matching haproxy.cfg's
    normalization exactly so both layers agree on route identity."""
    return _VERSION_PREFIX_RE.sub("", raw_path, count=1)


# HostConfig fields (unless noted) that could be used to escape the sandbox
# or defeat the airgap network/filesystem boundary. Reject the whole
# request if any is present in a form that grants something beyond the
# minimal executor container the application actually creates
# (container_manager.py:340-390 -- network="none", read_only=False for
# plot output, no capabilities added, no host mounts, user="executor").
_HOSTCONFIG_DENY_IF_TRUTHY = ("Privileged",)
_HOSTCONFIG_DENY_IF_NONEMPTY = (
    "Binds",
    "Mounts",
    "CapAdd",
    "Devices",
    "GroupAdd",
    "ExtraHosts",
    "SecurityOpt",  # blocks seccomp=unconfined / apparmor=unconfined overrides
    "Sysctls",
    "VolumesFrom",
    "Links",
    "CgroupParent",
)
_HOSTCONFIG_DENY_IF_HOST_MODE = ("PidMode", "IpcMode", "UTSMode")
_REQUIRED_NETWORK_MODE = "none"


def _reject_reason(body: dict) -> str | None:
    """Return a human-readable rejection reason, or None if the create
    request matches the shape the application's executor spawn actually
    uses."""
    image = body.get("Image")
    if _ALLOWED_IMAGES and image not in _ALLOWED_IMAGES:
        return f"Image {image!r} is not in the allowed executor image list"

    host_config = body.get("HostConfig") or {}
    if not isinstance(host_config, dict):
        return "HostConfig must be an object"

    for field in _HOSTCONFIG_DENY_IF_TRUTHY:
        if host_config.get(field):
            return f"HostConfig.{field} is not permitted"

    for field in _HOSTCONFIG_DENY_IF_NONEMPTY:
        value = host_config.get(field)
        if value:
            return f"HostConfig.{field} is not permitted"

    for field in _HOSTCONFIG_DENY_IF_HOST_MODE:
        value = host_config.get(field)
        if isinstance(value, str) and value.lower() == "host":
            return f"HostConfig.{field}=host is not permitted"

    network_mode = host_config.get("NetworkMode")
    if network_mode != _REQUIRED_NETWORK_MODE:
        return f"HostConfig.NetworkMode must be {_REQUIRED_NETWORK_MODE!r}, got {network_mode!r}"

    networking_config = body.get("NetworkingConfig")
    if networking_config and networking_config.get("EndpointsConfig"):
        return "NetworkingConfig.EndpointsConfig is not permitted"

    restart_policy = host_config.get("RestartPolicy") or {}
    restart_name = restart_policy.get("Name", "no") if isinstance(restart_policy, dict) else "no"
    if restart_name not in ("", "no"):
        return f"HostConfig.RestartPolicy.Name={restart_name!r} is not permitted"

    return None


async def _forward(
    session: ClientSession, request: web.Request, body: bytes | None = None
) -> web.StreamResponse:
    """Proxy `request` to the backend unchanged (or with `body` substituted),
    and relay the backend's response back unchanged."""
    target_url = f"{_BACKEND_BASE_URL}{request.rel_url}"
    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }
    payload = body if body is not None else await request.read()
    async with session.request(
        request.method,
        target_url,
        headers=forward_headers,
        data=payload,
    ) as backend_response:
        response_body = await backend_response.read()
        return web.Response(
            status=backend_response.status,
            headers={
                k: v
                for k, v in backend_response.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding")
            },
            body=response_body,
        )


async def handle_request(request: web.Request) -> web.StreamResponse:
    path = _normalized_path(request.path)
    session: ClientSession = request.app["session"]

    if _EXEC_PATH_RE.match(path):
        logger.warning("Denied exec-path request: %s %s", request.method, request.path)
        return web.Response(status=403, text="exec is not permitted in air-gapped mode\n")

    if request.method == "POST" and path == "/containers/create":
        # Docker's create body is always a plain Content-Length-framed JSON
        # object from the Python SDK -- never chunked. Reject anything that
        # arrives chunked for this route rather than trying to reconcile
        # two different framing interpretations between this layer and
        # haproxy behind it.
        if "chunked" in request.headers.get("Transfer-Encoding", "").lower():
            logger.warning("Denied chunked create request")
            return web.Response(status=400, text="chunked create bodies are not supported\n")

        raw_body = await request.read()
        try:
            parsed = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid JSON body\n")

        reason = _reject_reason(parsed)
        if reason is not None:
            logger.warning("Denied create request: %s", reason)
            return web.Response(status=403, text=f"{reason}\n")

        return await _forward(session, request, body=raw_body)

    return await _forward(session, request)


async def _make_app() -> web.Application:
    app = web.Application()
    app["session"] = ClientSession(timeout=ClientTimeout(total=600))
    app.router.add_route("*", "/{tail:.*}", handle_request)

    async def _close_session(app: web.Application) -> None:
        await app["session"].close()

    app.on_cleanup.append(_close_session)
    return app


if __name__ == "__main__":
    web.run_app(_make_app(), host="0.0.0.0", port=2375)

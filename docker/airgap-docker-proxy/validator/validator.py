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
import posixpath
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
# plot output, no capabilities added, user="executor").
#
# Binds/Mounts are NOT in this list -- found live (2026-07-10, first real
# torpor-job test against this proxy) that container_manager.py's
# _build_volumes() legitimately mounts the job's output directory (rw) and
# uploaded data-file directories (ro) for every real execute_code call.
# Blanket-denying any bind broke every single request. The actual invariant
# worth enforcing is that none of those mounts target a sensitive HOST path
# (see _FORBIDDEN_BIND_SOURCES / _reject_bind_reason below), not that there
# are zero mounts at all -- bind mounts are how this feature legitimately
# gets the agent's data in and its plots out.
_HOSTCONFIG_DENY_IF_TRUTHY = ("Privileged",)
_HOSTCONFIG_DENY_IF_NONEMPTY = (
    "CapAdd",
    "Devices",
    "GroupAdd",
    "ExtraHosts",
    "Sysctls",
    "VolumesFrom",
    "Links",
    "CgroupParent",
)
_HOSTCONFIG_DENY_IF_HOST_MODE = ("PidMode", "IpcMode", "UTSMode")
_REQUIRED_NETWORK_MODE = "none"

# SecurityOpt is NOT in _HOSTCONFIG_DENY_IF_NONEMPTY -- found live
# (2026-07-10, same torpor-job test that caught the Binds bug):
# container_manager.py's real containers.run() call sets
# security_opt=["no-new-privileges:true"] on every executor spawn, a
# hardening flag (blocks setuid-style privilege escalation), not a
# danger. Blanket-denying any non-empty SecurityOpt broke every request.
#
# Adversarial review considered a denylist of dangerous values (mirroring
# the Binds fix) and rejected it: Docker's seccomp/apparmor options accept
# either a well-known keyword ("unconfined") OR an arbitrary custom
# profile (a file path, or an inline JSON profile body for seccomp) --
# a maximally-permissive custom profile achieves the same effect as
# "unconfined" without containing that substring anywhere, so no
# substring denylist can be complete for these two options in principle,
# not just incomplete in a fixable way. Since the legitimate use case is
# exactly one fixed value, always, an allowlist has no downside here
# (unlike Binds, whose legitimate sources are genuinely variable paths).
_SECURITY_OPT_ALLOWED = frozenset({"no-new-privileges:true"})


def _reject_security_opt_reason(host_config: dict) -> str | None:
    security_opt = host_config.get("SecurityOpt") or []
    if not isinstance(security_opt, list):
        return "HostConfig.SecurityOpt must be an array"
    for entry in security_opt:
        if not isinstance(entry, str) or entry not in _SECURITY_OPT_ALLOWED:
            return f"HostConfig.SecurityOpt entry {entry!r} is not permitted"
    return None


# Host paths a bind/mount source must not be, or be an ancestor of. Blocks
# information disclosure (reading host secrets, even read-only) and sandbox
# escape (writing to host-sensitive locations) without blocking the
# legitimate per-job temp/data directories this feature actually needs,
# which live under the operator's OpenScientist checkout or the system
# temp directory -- neither of which is a fixed, portable path we can
# allowlist by prefix across deployments, so we deny-list the dangerous
# roots instead.
#
# Adversarial review (2026-07-10, pre-deployment) found macOS aliases
# /etc, /tmp, /var to /private/etc, /private/tmp, /private/var via
# symlinks -- both forms must be listed since this validator does
# string-only matching and cannot resolve symlinks (it has no host
# filesystem access; see _normalize_bind_source's docstring for why that's
# an accepted residual gap once these specific known aliases are covered).
#
# TODO(follow-up, not this PR): the same review recommended replacing this
# denylist with an allowlist keyed off OPENSCIENTIST_HOST_PROJECT_DIR plus
# a fixed executor-output-root env var (container_manager.py currently
# falls back to tempfile.mkdtemp(), an arbitrary OS temp path, when no
# output_dir is supplied -- not a fixed prefix an allowlist could pin
# today). That's an application-code change beyond this proxy's scope;
# tracked as a follow-up rather than blocking this fix.
_FORBIDDEN_BIND_SOURCES = frozenset(
    {
        "/",
        "/etc",
        "/root",
        "/home",
        "/boot",
        "/dev",
        "/proc",
        "/sys",
        "/var",
        "/var/run",
        "/var/run/docker.sock",
        "/run",
        "/run/docker.sock",
        # macOS Docker Desktop: /etc, /tmp, /var are symlinks to these.
        "/private",
        "/private/etc",
        "/private/var",
        "/private/var/run",
        "/private/var/run/docker.sock",
    }
)


def _normalize_bind_source(source: str) -> str:
    """Collapse `..`/`.`/redundant-slash segments the same way the kernel
    resolves them at mount time, so a structurally-different-looking
    string that resolves to a forbidden path doesn't evade string
    matching.

    Adversarial review (2026-07-10) found the previous version (bare
    `rstrip("/")`, no traversal collapsing) let
    "/tmp/../var/run/docker.sock" sail through -- neither an exact match
    nor a prefix match against `/var/run/docker.sock` as a literal
    string, but the kernel resolves the `..` and mounts the real socket.
    `posixpath.normpath` fixes that, but has its own gap the same review
    caught: POSIX explicitly permits (and Python's posixpath implements)
    treating a path with EXACTLY two leading slashes as implementation-
    defined and leaves it untouched -- `normpath("//etc")` returns
    `"//etc"`, not `"/etc"`. Linux's actual mount/path resolution does not
    special-case `//`; `//etc` and `/etc` are identical on the systems
    this proxy runs on. Collapse leading slashes ourselves before handing
    off to normpath so the two-slash case matches Linux reality, not the
    POSIX-spec ambiguity Python's stdlib preserves.

    Does NOT resolve symlinks (no filesystem access here), which is why
    known symlink aliases (macOS's /private/*) are separately listed in
    `_FORBIDDEN_BIND_SOURCES` rather than relied on to collapse away.
    """
    if source.startswith("/"):
        source = "/" + source.lstrip("/")
    normalized = posixpath.normpath(source)
    return normalized if normalized != "." else "/"


def _bind_sources(host_config: dict) -> list[str]:
    """Extract every mount source path from HostConfig.Binds (legacy
    "source:target[:mode]" strings), HostConfig.Mounts' "Source" field
    (bind/volume-type mounts), AND HostConfig.Mounts' volume-driver
    "device" option -- the local volume driver's `type=none,o=bind,
    device=<path>` form bind-mounts an arbitrary host path while
    `Mounts[].Source` itself is just a volume *name*, not a path. Missing
    this third channel was a critical, review-confirmed bypass: a
    "Type": "volume" mount with a bind-shaped local-driver device option
    reached the real Docker socket and never touched the Binds/Source
    checks at all.
    """
    sources: list[str] = []
    for entry in host_config.get("Binds") or []:
        if not isinstance(entry, str):
            continue
        # source:target or source:target:mode -- source is everything before
        # the first colon (bind sources are absolute paths, never contain
        # a colon themselves on Linux).
        source = entry.split(":", 1)[0]
        sources.append(source)
    for entry in host_config.get("Mounts") or []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("Source"), str):
            sources.append(entry["Source"])
        volume_options = entry.get("VolumeOptions")
        if isinstance(volume_options, dict):
            driver_config = volume_options.get("DriverConfig")
            if isinstance(driver_config, dict):
                options = driver_config.get("Options")
                if isinstance(options, dict) and isinstance(options.get("device"), str):
                    sources.append(options["device"])
    return sources


def _reject_bind_reason(host_config: dict) -> str | None:
    for raw_source in _bind_sources(host_config):
        normalized = _normalize_bind_source(raw_source)
        if normalized in _FORBIDDEN_BIND_SOURCES:
            return f"bind source {raw_source!r} is a forbidden host path"
        for forbidden in _FORBIDDEN_BIND_SOURCES:
            if forbidden != "/" and normalized.startswith(forbidden + "/"):
                return f"bind source {raw_source!r} is under forbidden host path {forbidden!r}"
    return None


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

    bind_reason = _reject_bind_reason(host_config)
    if bind_reason is not None:
        return bind_reason

    security_opt_reason = _reject_security_opt_reason(host_config)
    if security_opt_reason is not None:
        return security_opt_reason

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

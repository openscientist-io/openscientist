"""Docker socket selection for air-gapped mode.

Per RFC §9: in air-gap mode, executor container spawns go through an
operator-deployed Docker socket proxy instead of the real
``/var/run/docker.sock``. The proxy enforces a hard-coded default-deny
policy: only the few operations OpenScientist's executor lifecycle needs
(create, start, wait, logs, remove for executor-labeled containers) pass
through; everything else (``exec`` / ``cp`` / ``inspect`` on unrelated
containers, ``network connect``, image pull/build, volume ops, host PID/IPC
mounts, ``--privileged``, ``--cap-add``, ``--device``) is rejected.

The proxy implementation choice is RFC §19 OQ#8 — ``tecnativa/docker-
socket-proxy`` image vs. a custom ``socat``+filter wrapper. Either way, the
operator stands it up before starting OpenScientist (documented in
``docs/AIR_GAPPED.md``).

Host-side vs. container-side socket paths
-----------------------------------------

Codex Review-7 BUG #2 (fixed): the prior implementation conflated two
distinct paths:

* ``settings.airgap.docker_socket_path`` — the **host-side** path the
  operator's proxy listens on (e.g. ``/var/run/airgap-docker.sock``). The
  runner uses this as the host side of the agent container's bind-mount.
* The **container-side** path where the docker SDK looks for the socket
  inside the process. This is always ``/var/run/docker.sock`` by
  convention; the operator's docker-compose mounts the host proxy socket
  to this conventional path inside the web container.

The prior version pointed :class:`docker.DockerClient` at the host-side
path from inside the web container — that path doesn't exist there. The
fix is to always use the conventional container-side path
(``/var/run/docker.sock``). The host-side ``docker_socket_path`` setting
is now host-only (consumed by the runner for the agent container's mount,
not by the container_manager).
"""

from __future__ import annotations

import os
from typing import Any

# Conventional container-side Unix socket path the docker SDK looks for.
# Operators mount their proxy here in the web container.
_CONTAINER_DOCKER_SOCKET = "/var/run/docker.sock"

# TCP override for environments where bind-mounting the proxy's Unix socket
# into a container isn't reliable. On Docker Desktop for macOS the file-
# sharing layer represents a bind-mounted Unix socket as a socket-typed
# inode but rejects connect() with ECONNREFUSED — a known limitation that
# blocks the operator-deployed proxy from being reachable from the agent
# via the conventional path. Setting this env var to ``host:port`` makes
# the docker SDK speak HTTP/TCP to the proxy directly; on Linux deploys
# this should stay unset and the Unix socket path is used.
_TCP_OVERRIDE_ENV = "OPENSCIENTIST_AIRGAP_DOCKER_TCP"


def docker_base_url_for_airgap(settings: Any) -> str:
    """Return the ``base_url`` to hand to ``docker.DockerClient`` in airgap.

    Default: a ``unix://`` URI pointing at the conventional container-side
    socket (``/var/run/docker.sock``). The operator's docker-compose
    mounts the airgap proxy socket to that path inside the web container;
    inside the container the docker SDK sees a normal socket and never
    knows it's a proxy. Right for production Linux deploys.

    Override: if ``OPENSCIENTIST_AIRGAP_DOCKER_TCP`` is set, return a
    ``tcp://`` URI pointing at the specified ``host:port``. Necessary on
    Docker Desktop for macOS (and any other host where bind-mounting a
    Unix socket into a container doesn't work reliably), where the
    operator's proxy must be reached over TCP instead.

    The ``settings.airgap.docker_socket_path`` setting is the **host-side**
    path used by the runner for the agent container's bind-mount; it's
    irrelevant here.

    Args:
        settings: Unused; retained for API compat.

    Returns:
        A ``unix://`` or ``tcp://`` URI suitable for the docker SDK's
        ``base_url`` argument.
    """
    del settings  # not used; see docstring
    tcp = os.environ.get(_TCP_OVERRIDE_ENV, "").strip()
    if tcp:
        return f"tcp://{tcp}"
    return f"unix://{_CONTAINER_DOCKER_SOCKET}"

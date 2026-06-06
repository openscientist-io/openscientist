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
``docs/AIR_GAPPED.md``); this module's only job is to point
:class:`ContainerManager` at the configured socket.

The application's only enforcement is refusing to point at
``/var/run/docker.sock`` when air-gap mode is on (already validated in
:class:`AirgapSettings`); the actual deny-listing happens at the proxy.
"""

from __future__ import annotations

from typing import Any


def docker_base_url_for_airgap(settings: Any) -> str:
    """Return the ``base_url`` to hand to ``docker.DockerClient`` in airgap.

    Reads ``settings.airgap.docker_socket_path``; the caller passes this to
    :class:`docker.DockerClient`'s constructor instead of using
    :func:`docker.from_env`.

    Args:
        settings: The OS settings object. Must have
            ``airgap.docker_socket_path``.

    Returns:
        A ``unix://`` URI suitable for the docker SDK's ``base_url`` argument.
    """
    return f"unix://{settings.airgap.docker_socket_path}"

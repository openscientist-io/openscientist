"""Binding the web's side listeners, the LLM proxy and the execution broker."""

from __future__ import annotations

import socket


class ListenerBindError(RuntimeError):
    """A side listener cannot bind its port, and agent jobs need it, so the web
    must not start without it."""


def bind_listener(port: int, *, name: str, setting: str) -> socket.socket:
    """A socket bound to ``port`` on every interface, for uvicorn to serve on.

    Raises:
        ListenerBindError: The port cannot be bound. The message names the
            listener, the port, the reason and ``setting``, which moves it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as e:
        sock.close()
        raise ListenerBindError(
            f"The {name} cannot listen on port {port} ({e.strerror}). Set {setting} to a free port."
        ) from e
    return sock

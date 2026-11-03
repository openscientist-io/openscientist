"""The unix-socket listener: file mode 0660 for the web, SO_PEERCRED as the gate."""

from __future__ import annotations

import os
import socket
import struct

from openscientist_launcher.config import LauncherConfig


def peer_credential(conn: socket.socket) -> tuple[int, int, int]:
    """(pid, uid, gid) of the process on the other end of the connection."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3I"))
    return struct.unpack("3I", raw)


def credential_accepted(uid: int, gid: int, web_uid: int, web_gid: int) -> bool:
    """The web identifies by both uid and gid; a matching supplementary group
    alone is not the web."""
    return uid == web_uid and gid == web_gid


class GuardedUnixSocket(socket.socket):
    """A listening unix socket that refuses connections from the wrong peer.

    asyncio calls ``accept`` on this object for every connection, so the
    refusal happens before the HTTP layer sees anything: the client's
    connection is reset and the request can never reach the app.
    """

    def __init__(self, web_uid: int, web_gid: int) -> None:
        super().__init__(socket.AF_UNIX, socket.SOCK_STREAM)
        self._web_uid = web_uid
        self._web_gid = web_gid

    def accept(self) -> tuple[socket.socket, object]:
        conn, addr = super().accept()
        try:
            _pid, uid, gid = peer_credential(conn)
            if not credential_accepted(uid, gid, self._web_uid, self._web_gid):
                raise ConnectionAbortedError("caller is not the web")
        except OSError:
            conn.close()
            raise
        return conn, addr


def create_listening_socket(cfg: LauncherConfig) -> GuardedUnixSocket:
    """Bind the guarded socket; the parent directory is created when absent."""
    sock = GuardedUnixSocket(cfg.web_uid, cfg.web_gid)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    path = cfg.socket_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    sock.bind(str(path))
    sock.listen(128)
    os.fchmod(sock.fileno(), 0o660)
    try:
        os.fchown(sock.fileno(), cfg.web_uid, cfg.web_gid)
    except PermissionError:
        # A non-root launcher cannot chown; the peer check is the real gate.
        pass
    return sock

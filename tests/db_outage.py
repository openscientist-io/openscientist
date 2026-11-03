"""A real database outage for tests: the app reaches the test database through
a TCP forwarder that a test closes, so connects are refused, or hangs, so
connects and queries wait without an answer, and reopens."""

from __future__ import annotations

import socket
import threading


class Forwarder:
    """Forwards a local port to the database until :meth:`down` or :meth:`hang`,
    and again after :meth:`up`."""

    def __init__(self, target: tuple[str, int]) -> None:
        self._target = target
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self._listener: socket.socket | None = None
        self._open: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._flowing = threading.Event()
        self._flowing.set()
        self.up()

    def up(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen()
        listener.settimeout(0.1)
        self._listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def hang(self) -> None:
        """Keep accepting connects and holding connections, but pass no bytes
        until :meth:`down`, like a paused server."""
        self._flowing.clear()

    def down(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        with self._lock:
            for sock in self._open:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self._open.clear()
        self._flowing.set()

    def _accept(self, listener: socket.socket) -> None:
        while self._listener is listener:
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            server = socket.create_connection(self._target)
            with self._lock:
                self._open.update((client, server))
            for src, dst in ((client, server), (server, client)):
                threading.Thread(target=self._pipe, args=(src, dst), daemon=True).start()

    def _pipe(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                self._flowing.wait()
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

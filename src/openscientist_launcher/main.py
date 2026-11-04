"""Serve the launcher API on the guarded unix socket."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from contextlib import suppress

import uvicorn

import docker
from openscientist_launcher.api import create_app
from openscientist_launcher.config import ConfigError, LauncherConfig
from openscientist_launcher.host_paths import check_jobs_root
from openscientist_launcher.socket_auth import create_listening_socket

logger = logging.getLogger(__name__)


async def _run(server: uvicorn.Server, sock: socket.socket) -> None:
    await server.startup(sockets=[sock])
    try:
        await server.main_loop()
    finally:
        await server.shutdown(sockets=[sock])


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LAUNCHER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = LauncherConfig.from_env(os.environ)
    except ConfigError as exc:
        print(f"launcher configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    problem = check_jobs_root(cfg, docker.from_env())
    if problem is not None:
        print(f"launcher configuration error: {problem}", file=sys.stderr)
        sys.exit(2)

    app = create_app(cfg)
    sock = create_listening_socket(cfg)
    config = uvicorn.Config(app, log_level="warning")
    config.load()
    server = uvicorn.Server(config)
    # serve() wires the lifespan before startup; it is skipped here because
    # the socket is pre-bound and guarded, not one uvicorn created.
    server.lifespan = config.lifespan_class(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _exit() -> None:
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _exit)

    try:
        loop.run_until_complete(_run(server, sock))
    finally:
        loop.close()
        sock.close()
        with suppress(OSError):
            cfg.socket_path.unlink()
        logger.info("Launcher socket closed at %s", cfg.socket_path)

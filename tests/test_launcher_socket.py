"""The launcher socket file the web connects to, bound for real in a temp dir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openscientist_launcher.config import LauncherConfig
from openscientist_launcher.socket_auth import create_listening_socket


def _config(tmp_path: Path, web_gid: int) -> LauncherConfig:
    return LauncherConfig.from_env(
        {
            "JOBS_ROOT": str(tmp_path),
            "AGENT_IMAGE": "agent:latest",
            "EXECUTOR_IMAGE": "executor:latest",
            "AGENT_IMAGE_DIGESTS": "sha256:" + "a" * 64,
            "EXECUTOR_IMAGE_DIGESTS": "sha256:" + "b" * 64,
            "LAUNCHER_INSTANCE_ID": "test",
            "LAUNCHER_WEB_UID": str(os.getuid()),
            "LAUNCHER_WEB_GID": str(web_gid),
            "LAUNCHER_SOCKET_PATH": str(tmp_path / "run" / "launcher.sock"),
            "AGENT_MAX_MEMORY_MIB": "1024",
            "AGENT_MAX_CPU": "1",
            "EXECUTOR_MAX_MEMORY_MIB": "1024",
            "EXECUTOR_MAX_CPU": "1",
            "MAX_LIFETIME_SECONDS": "60",
        }
    )


def test_socket_file_is_the_webs_and_mode_0660(tmp_path: Path) -> None:
    """The bound path, which the web connects through, carries the web's group and 0660."""
    others = [gid for gid in os.getgroups() if gid != os.getgid()]
    if not others:
        pytest.skip("the test user needs a supplementary group to observe the chown")
    cfg = _config(tmp_path, others[0])
    sock = create_listening_socket(cfg)
    try:
        st = cfg.socket_path.stat()
    finally:
        sock.close()
    assert st.st_mode & 0o777 == 0o660
    assert (st.st_uid, st.st_gid) == (os.getuid(), others[0])

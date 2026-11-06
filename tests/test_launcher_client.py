"""How the web's launcher client reports a launcher that gives no answer."""

from __future__ import annotations

import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from openscientist.job_container import launcher_client
from openscientist.job_container.launcher_client import LauncherClient, LauncherUnavailableError


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A short directory, since a unix socket path is limited to 108 bytes."""
    path = Path(tempfile.mkdtemp(prefix="lc"))
    try:
        yield path
    finally:
        shutil.rmtree(path)


def test_a_launcher_that_accepts_but_never_answers_reads_as_a_timeout(
    socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_client, "_CONTROL_TIMEOUT_SECONDS", 0.2)
    path = socket_dir / "launcher.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)
        with pytest.raises(LauncherUnavailableError) as raised:
            LauncherClient(path).health()
    assert raised.value.code == "unavailable"
    assert raised.value.message == f"the launcher at {path} did not answer within 0.2s"


def test_a_socket_nobody_listens_on_reads_as_unreachable(socket_dir: Path) -> None:
    path = socket_dir / "launcher.sock"
    with pytest.raises(LauncherUnavailableError) as raised:
        LauncherClient(path).health()
    assert raised.value.code == "unavailable"
    assert raised.value.message == f"the launcher at {path} could not be reached"

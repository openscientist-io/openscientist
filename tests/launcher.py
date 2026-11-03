"""Start the real launcher process on a temp unix socket, the way compose does.

The launcher tests and the web-side cutover tests both drive the production
``openscientist_launcher`` module as a subprocess; this is the one place that
knows how.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

#: The marker label value every container a test launcher creates carries.
#: One per pytest process, so parallel sessions on the shared daemon never
#: list each other's containers.
LAUNCHER_INSTANCE_ID = f"test-instance-{os.getpid()}"

EXECUTOR_IMAGE = "openscientist-executor:latest"


def image_digest(docker_client, tag: str) -> str:
    return str(docker_client.images.get(tag).attrs["Id"])


class Launcher:
    """A launcher subprocess on a unix socket, started like compose would."""

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        socket_path: Path,
        env: dict[str, str],
        log: Path,
    ) -> None:
        self.proc = proc
        self.socket_path = socket_path
        self.env = env
        self.log = log

    @staticmethod
    def start(
        tmp_path: Path, jobs_root: Path, *, wait_healthy: bool = True, **env_overrides: str
    ) -> "Launcher":
        env = {
            "PATH": os.environ.get("PATH", ""),
            "JOBS_ROOT": str(jobs_root),
            # The executor tag stands in for the agent image by default; the
            # launcher tests override it with their slim test image.
            "AGENT_IMAGE": EXECUTOR_IMAGE,
            "EXECUTOR_IMAGE": EXECUTOR_IMAGE,
            "AGENT_IMAGE_DIGESTS": "",
            "EXECUTOR_IMAGE_DIGESTS": "",
            "LAUNCHER_INSTANCE_ID": LAUNCHER_INSTANCE_ID,
            "LAUNCHER_WEB_UID": str(os.getuid()),
            "LAUNCHER_WEB_GID": str(os.getgid()),
            "LAUNCHER_SOCKET_PATH": str(tmp_path / "launcher.sock"),
            "LAUNCHER_NETWORK": "bridge",
            "AGENT_MAX_MEMORY_MIB": "8192",
            "AGENT_MAX_CPU": "4",
            "EXECUTOR_MAX_MEMORY_MIB": "2048",
            "EXECUTOR_MAX_CPU": "1",
            "MAX_LIFETIME_SECONDS": "3600",
            "MAX_EGRESS_ENTRIES": "8",
        }
        env.update(env_overrides)
        log = tmp_path / "launcher.log"
        proc = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "openscientist_launcher"],
            cwd=REPO_ROOT,
            env=env,
            stdout=log.open("wb"),
            stderr=subprocess.STDOUT,
        )
        launcher = Launcher(proc, Path(env["LAUNCHER_SOCKET_PATH"]), env, log)
        if wait_healthy:
            launcher._wait_healthy()
        return launcher

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=str(self.socket_path)),
            base_url="http://launcher",
            timeout=120,
        )

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"launcher exited early:\n{self.log.read_text(errors='replace')}")
            try:
                response = self.client().get("/v1/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        pytest.fail(f"launcher never became healthy:\n{self.log.read_text(errors='replace')}")

    def wait_socket(self) -> None:
        # A launcher whose web uid is not ours refuses every connection, so the
        # socket file itself is the only observable "up" signal.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"launcher exited early:\n{self.log.read_text(errors='replace')}")
            if self.socket_path.exists():
                return
            time.sleep(0.2)
        pytest.fail(f"launcher socket never appeared:\n{self.log.read_text(errors='replace')}")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

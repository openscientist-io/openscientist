"""The start and stop command sequences through an injected command runner."""

import http.server
import threading
from collections.abc import Mapping
from pathlib import Path

from openscientist.cli import stack


def _recorded(run):
    return [argv for argv, _ in run]


def test_start_runs_the_four_builds_then_up_in_order(checkout: Path, monkeypatch):
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    assert stack.start(checkout, run) == 0
    argvs = [argv for argv, _ in calls]
    assert argvs[0] == (
        "docker",
        "build",
        "-f",
        "Dockerfile.base",
        "-t",
        "openscientist-base:latest",
        ".",
    )
    assert calls[0][1] == {"DOCKER_DEFAULT_PLATFORM": "linux/amd64"}
    assert argvs[1][:5] == ("docker", "compose", "-f", "docker-compose.yml", "build")
    assert argvs[1].count("--build-arg") == 2
    assert any(arg.startswith("OPENSCIENTIST_COMMIT=") for arg in argvs[1])
    assert any(arg.startswith("BUILD_TIME=") for arg in argvs[1])
    assert argvs[2] == (
        "docker",
        "build",
        "-f",
        "Dockerfile.executor",
        "-t",
        "openscientist-executor:latest",
        ".",
    )
    assert argvs[3] == (
        "docker",
        "build",
        "-f",
        "Dockerfile.agent",
        "-t",
        "openscientist-agent:latest",
        ".",
    )
    assert argvs[4] == (
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "up",
        "-d",
        "--remove-orphans",
        "openscientist",
    )


def test_start_stops_at_the_first_failing_build(checkout: Path, monkeypatch):
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 1 if len(calls) == 3 else 0

    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    assert stack.start(checkout, run) == 1
    assert len(calls) == 3
    assert "up" not in calls[-1][0]


def test_start_stops_when_up_fails(checkout: Path, monkeypatch):
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 7 if "up" in argv else 0

    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    assert stack.start(checkout, run) == 7
    assert len(calls) == 5


def test_start_fails_when_health_never_answers(checkout: Path, monkeypatch):
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: False)
    assert stack.start(checkout, run) == 1
    assert len(calls) == 5


def test_stop_runs_compose_down_and_propagates_the_exit_code(checkout: Path):
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    assert stack.stop(checkout, run) == 0
    assert [argv for argv, _ in calls] == [
        ("docker", "compose", "-f", "docker-compose.yml", "down", "--remove-orphans")
    ]
    assert stack.stop(checkout, lambda argv, env: 3) == 3


def test_wait_for_health_polls_until_the_server_answers(checkout: Path):
    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert stack.wait_for_health(
            f"http://127.0.0.1:{server.server_address[1]}", timeout=5, interval=0.1
        )
    finally:
        server.shutdown()
        server.server_close()
    assert not stack.wait_for_health("http://127.0.0.1:1", timeout=1, interval=0.1)

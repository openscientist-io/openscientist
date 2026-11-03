"""The start and stop command sequences through an injected command runner."""

import http.server
import re
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from openscientist.cli import stack
from tests.cli.conftest import REPO_ROOT


def _recorded(run):
    return [argv for argv, _ in run]


def test_start_runs_the_four_builds_then_up_in_order(checkout: Path, monkeypatch):
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    monkeypatch.setattr(stack, "_image_id", lambda image: "sha256:" + "f" * 64)
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
    assert calls[1][1] == {"DOCKER_DEFAULT_PLATFORM": "linux/amd64"}
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


def test_image_tags_come_from_the_env_file(checkout: Path, monkeypatch: pytest.MonkeyPatch):
    (checkout / ".env").write_text(
        "OPENSCIENTIST_BASE_IMAGE=openscientist-base:staging\n"
        "OPENSCIENTIST_EXECUTOR_IMAGE=openscientist-executor:staging\n"
        "OPENSCIENTIST_AGENT_IMAGE=openscientist-agent:staging\n",
        encoding="utf-8",
    )
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(stack, "_image_id", lambda image: "sha256:" + "e" * 64)
    assert stack.build_images(run, checkout) == 0
    tags = [argv[argv.index("-t") + 1] for argv, _ in calls if "-t" in argv]
    assert tags == [
        "openscientist-base:staging",
        "openscientist-executor:staging",
        "openscientist-agent:staging",
    ]
    owner = stack.envfiles.file_values(checkout, "owner")
    assert owner["AGENT_IMAGE_DIGESTS"] == "sha256:" + "e" * 64
    assert owner["EXECUTOR_IMAGE_DIGESTS"] == "sha256:" + "e" * 64


def test_image_tags_fall_back_to_latest_when_unset_or_empty(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    (checkout / ".env").write_text(
        "OPENSCIENTIST_BASE_IMAGE=\nOPENSCIENTIST_EXECUTOR_IMAGE=\nOPENSCIENTIST_AGENT_IMAGE=\n",
        encoding="utf-8",
    )
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(stack, "_image_id", lambda image: "sha256:" + "d" * 64)
    assert stack.build_images(run, checkout) == 0
    tags = [argv[argv.index("-t") + 1] for argv, _ in calls if "-t" in argv]
    assert tags == [
        "openscientist-base:latest",
        "openscientist-executor:latest",
        "openscientist-agent:latest",
    ]
    owner = stack.envfiles.file_values(checkout, "owner")
    assert owner["AGENT_IMAGE_DIGESTS"] == "sha256:" + "d" * 64
    assert owner["EXECUTOR_IMAGE_DIGESTS"] == "sha256:" + "d" * 64


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


def test_run_command_returns_the_exit_code_and_merges_the_env(checkout: Path):
    assert stack.run_command(("true",)) == 0
    assert stack.run_command(("false",)) == 1
    assert stack.run_command(("sh", "-c", 'test "$FOO" = bar'), {"FOO": "bar"}) == 0


@pytest.mark.parametrize("failing_call", [1, 2, 4])
def test_build_images_stops_at_the_failing_build(checkout: Path, failing_call: int):
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append(argv)
        return 3 if len(calls) == failing_call else 0

    assert stack.build_images(run, checkout) == 3
    assert len(calls) == failing_call


def test_commit_reports_the_short_head_sha(checkout: Path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    assert re.fullmatch(r"[0-9a-f]{7,40}", stack._commit())


def _start_with(checkout: Path, monkeypatch, airgapped: str, count: int, accept: bool):
    """Run stack.start with the mirror count and the offer answer controlled.

    Returns the executed commands and the prompts the mirror offer showed.
    """
    (checkout / ".env").write_text(
        f"OPENSCIENTIST_PROVIDER=vllm\nOPENSCIENTIST_AIRGAPPED={airgapped}\n",
        encoding="utf-8",
    )
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []
    prompts: list[str] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    def confirm(prompt: str) -> bool:
        prompts.append(prompt)
        return accept

    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    monkeypatch.setattr(stack, "pubmed_mirror_count", lambda root: count)
    monkeypatch.setattr(stack, "_confirm", confirm)
    assert stack.start(checkout, run) == 0
    return calls, prompts


def _loaded(calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]]) -> bool:
    return any("openscientist.pubmed_mirror" in " ".join(argv) for argv, _ in calls)


def test_mirror_offered_when_airgapped_and_empty(checkout: Path, monkeypatch):
    calls, prompts = _start_with(checkout, monkeypatch, airgapped="true", count=0, accept=False)
    # The offer is made but declined, so the load command never runs.
    assert prompts and prompts[0].startswith("Load the PubMed mirror")
    assert not _loaded(calls)


def test_mirror_loaded_when_the_offer_is_accepted(checkout: Path, monkeypatch):
    calls, prompts = _start_with(checkout, monkeypatch, airgapped="true", count=0, accept=True)
    assert prompts
    assert _loaded(calls)


def test_mirror_not_offered_when_airgapped_is_off(checkout: Path, monkeypatch):
    calls, prompts = _start_with(checkout, monkeypatch, airgapped="false", count=0, accept=True)
    assert prompts == []
    assert not _loaded(calls)


def test_mirror_not_offered_when_not_empty(checkout: Path, monkeypatch):
    calls, prompts = _start_with(checkout, monkeypatch, airgapped="true", count=7, accept=True)
    assert prompts == []
    assert not _loaded(calls)


def test_mirror_count_reads_the_psql_row_count(checkout: Path, monkeypatch):
    seen: list[tuple[str, ...]] = []

    class _Proc:
        returncode = 0
        stdout = "7\n"

    def fake_run(argv, *args, **kwargs):
        seen.append(argv)
        return _Proc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    monkeypatch.setattr(stack.envfiles, "file_values", lambda root, file: {})
    assert stack.pubmed_mirror_count(checkout) == 7
    # Without a .env the default user and database are used for the psql login.
    assert seen[0][7:] == (
        "psql",
        "-U",
        "openscientist",
        "-d",
        "openscientist",
        "-tAc",
        "SELECT count(*) FROM pubmed_articles;",
    )


def test_mirror_count_is_none_when_the_row_count_is_unreadable(checkout: Path, monkeypatch):
    (checkout / ".env").write_text(
        "POSTGRES_USER=custuser\nPOSTGRES_DB=customdb\n", encoding="utf-8"
    )
    seen: list[tuple[str, ...]] = []

    class _FailedProc:
        returncode = 1
        stdout = "psql: connection refused"

    class _EmptyProc:
        returncode = 0
        stdout = ""

    def fake_run(argv, *args, **kwargs):
        seen.append(argv)
        return _FailedProc()

    monkeypatch.setattr(stack.subprocess, "run", fake_run)
    assert stack.pubmed_mirror_count(checkout) is None
    assert seen[0][7:12] == ("psql", "-U", "custuser", "-d", "customdb")
    # A failed psql prints nothing, and neither does a successful one that
    # prints no rows; both read as "cannot be read".
    monkeypatch.setattr(stack.subprocess, "run", lambda argv, *args, **kwargs: _EmptyProc())
    assert stack.pubmed_mirror_count(checkout) is None


def test_mirror_load_failure_is_not_fatal(checkout: Path, monkeypatch):
    (checkout / ".env").write_text(
        "OPENSCIENTIST_PROVIDER=vllm\nOPENSCIENTIST_AIRGAPPED=true\n", encoding="utf-8"
    )
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        calls.append((argv, env))
        return 9 if "openscientist.pubmed_mirror" in " ".join(argv) else 0

    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    monkeypatch.setattr(stack, "pubmed_mirror_count", lambda root: 0)
    monkeypatch.setattr(stack, "_confirm", lambda prompt: True)
    assert stack.start(checkout, run) == 0
    assert _loaded(calls)


def test_confirm_reads_the_yes_no_answer(monkeypatch):
    import click

    def fixed_prompt(value: str):
        def prompt(*args, **kwargs):
            return value

        return prompt

    for answer, expected in (("y", True), ("yes", True), ("n", False), ("", False)):
        monkeypatch.setattr(click, "prompt", fixed_prompt(answer))
        assert stack._confirm("Load the PubMed mirror now? (y/N) ") is expected

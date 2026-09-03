"""Reproducible local quality gates matching the repository CI tiers."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Protocol

BLOCKED_EXIT_CODE = 2
TESTS_DIR = "tests/"

# Public, backend-neutral contracts that do not require PostgreSQL or Docker.
CONTRACT_TESTS = (
    "tests/agent/test_backend_exhaustiveness.py",
    "tests/agent/test_factory.py",
    "tests/agent/test_mcp_specs.py",
    "tests/transcript",
    "tests/test_models.py",
    "tests/test_prompts.py",
    "tests/test_providers.py",
    "tests/test_token_usage.py",
    "tests/test_version.py",
)


class DockerClient(Protocol):
    """Small Docker client surface required by the integration preflight."""

    def ping(self) -> bool: ...

    def close(self) -> None: ...


def _open_docker_client() -> DockerClient:
    import docker

    return docker.from_env()


def require_docker(
    client_factory: Callable[[], DockerClient] = _open_docker_client,
) -> int:
    """Return a clear blocker before pytest attempts to start containers."""

    client: DockerClient | None = None
    try:
        client = client_factory()
        if not client.ping():
            raise RuntimeError("Docker daemon ping returned false")
    except Exception as exc:
        print(
            "[quality-integration] BLOCKED: Docker daemon is unavailable "
            f"({type(exc).__name__}). Start Docker Desktop or configure DOCKER_HOST, "
            "then rerun `make quality-integration`.",
            file=sys.stderr,
            flush=True,
        )
        return BLOCKED_EXIT_CODE
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()

    print("[quality-integration] Docker daemon is available.", flush=True)
    return 0


def _run_commands(
    label: str,
    commands: Sequence[Sequence[str]],
    *,
    env: dict[str, str] | None = None,
) -> int:
    for command in commands:
        rendered = subprocess.list2cmdline(list(command))
        print(f"[{label}] $ {rendered}", flush=True)
        result = subprocess.run(command, check=False, env=env)
        if result.returncode:
            print(
                f"[{label}] FAILED with exit code {result.returncode}: {rendered}",
                file=sys.stderr,
                flush=True,
            )
            return result.returncode
    print(f"[{label}] PASSED", flush=True)
    return 0


def run_fast() -> int:
    """Run deterministic, Docker-free checks used before longer test tiers."""

    return _run_commands(
        "quality-fast",
        (
            ("uv", "lock", "--check"),
            ("git", "-c", "core.safecrlf=false", "diff", "--check"),
            (sys.executable, "-m", "compileall", "-q", "src"),
            (sys.executable, "-m", "ruff", "check", "src/", TESTS_DIR),
            (sys.executable, "-m", "ruff", "format", "--check", "src/", TESTS_DIR),
            (sys.executable, "-m", "mypy", "src/openscientist/", TESTS_DIR),
        ),
    )


def run_contract() -> int:
    """Run backend-neutral API and serialization contracts without Docker."""

    return _run_commands(
        "quality-contract",
        ((sys.executable, "-m", "pytest", "-q", *CONTRACT_TESTS),),
    )


def run_integration() -> int:
    """Run the full coverage suite after an explicit Docker daemon preflight."""

    docker_status = require_docker()
    if docker_status:
        return docker_status
    integration_env = None
    if sys.platform == "win32":
        # Docker Desktop can run test containers while failing to publish
        # Ryuk's control port. Context managers still remove their containers.
        integration_env = {**os.environ, "TESTCONTAINERS_RYUK_DISABLED": "true"}
    return _run_commands(
        "quality-integration",
        (
            (
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "not network",
                "--cov=src/openscientist",
                "--cov-report=term-missing",
            ),
        ),
        env=integration_env,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=("fast", "contract", "integration"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runners = {
        "fast": run_fast,
        "contract": run_contract,
        "integration": run_integration,
    }
    return runners[args.tier]()


if __name__ == "__main__":
    raise SystemExit(main())

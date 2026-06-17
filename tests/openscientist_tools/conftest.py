"""Shared pytest fixtures for spawning the standalone tools MCP server."""

from __future__ import annotations

import os

# Required env vars for `openscientist_tools.state.STATE` to instantiate at
# import time. Tests that need a specific job_id mutate `state.STATE.job_id`
# at runtime; this just gives state.py something to bind to.
os.environ.setdefault("OPENSCIENTIST_JOB_ID", "test-placeholder")
os.environ.setdefault("OPENSCIENTIST_JOB_DIR", "/tmp")

import sys  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402

from openscientist.knowledge_state import KnowledgeState  # noqa: E402


@pytest.fixture
def server_env() -> Callable[..., dict[str, str]]:
    """Return a builder for a clean spawn-env dict.

    Strips any pre-existing ``OPENSCIENTIST_*`` keys from the parent
    env so tests are hermetic, then applies sane defaults plus the
    caller's overrides. Pass ``OPENSCIENTIST_JOB_DIR=""`` to drop a
    required var.
    """

    def _build(job_dir: Path, **overrides: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENSCIENTIST_")}
        env["OPENSCIENTIST_JOB_ID"] = "test-job-001"
        env["OPENSCIENTIST_JOB_DIR"] = str(job_dir)
        # Provider has no default, so the subprocess must be given one explicitly
        # (CI has no .env to fall back on). These tools never call the LLM.
        env["OPENSCIENTIST_PROVIDER"] = "anthropic"
        for key, value in overrides.items():
            if value == "":
                env.pop(key, None)
            else:
                env[key] = value
        return env

    return _build


@pytest.fixture
def server_params() -> Callable[[dict[str, str]], StdioServerParameters]:
    def _build(env: dict[str, str]) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "openscientist_tools"],
            env=env,
        )

    return _build


@pytest.fixture
def patched_ks_persistence(monkeypatch: pytest.MonkeyPatch) -> KnowledgeState:
    """Replace `KnowledgeState.{load,save}_from_database_sync` with
    in-memory no-ops and return a real `KnowledgeState` instance that
    the tool body mutates."""
    real_ks = KnowledgeState(
        job_id="test-job-uuid",
        research_question="placeholder question",
        max_iterations=1,
    )
    monkeypatch.setattr(
        KnowledgeState,
        "load_from_database_sync",
        classmethod(lambda cls, job_id, user_id=None: real_ks),
    )
    monkeypatch.setattr(
        KnowledgeState,
        "save_to_database_sync",
        lambda self, job_id, user_id=None: None,
    )
    return real_ks

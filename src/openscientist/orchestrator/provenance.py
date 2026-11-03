"""Runtime version provenance for a discovery job."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from uuid import UUID

from sqlalchemy import text, update

from openscientist.agent.base import AgentBackend
from openscientist.agent.factory import agent_class_for_provider_id
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import async_session_local
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

# Provenance is a single small update, so anything slower is a database in
# trouble, and the run should carry on without the stamp. The statement bound
# lives in the database. The deadline covers what the database cannot see.
_STAMP_TIMEOUT_MS = 10_000
_STAMP_DEADLINE_SECONDS = 30.0


async def _sync_version_metadata(job_id: str) -> None:
    """Record runtime version provenance on the job, best effort.

    Two bounds apply to the write because neither covers the other.
    ``statement_timeout`` stops a slow statement inside the database, and the
    surrounding deadline covers what the database cannot see (name resolution,
    connecting, acquiring a connection, cleanup). Metadata collection runs in
    a worker thread, and a failure there is still caught here.
    """
    try:
        async with asyncio.timeout(_STAMP_DEADLINE_SECONDS):
            version_info = await asyncio.to_thread(get_version_metadata)
            if not version_info:
                return
            async with async_session_local() as session:
                await session.execute(text(f"SET LOCAL statement_timeout = {_STAMP_TIMEOUT_MS}"))
                await session.execute(
                    update(JobModel)
                    .where(JobModel.id == UUID(job_id))
                    .values(version_info=version_info)
                )
                await session.commit()
    except Exception as e:
        logger.warning("Could not record version provenance for job %s: %s", job_id, e)


def _harness_binary(harness: AgentBackend) -> str:
    """The binary the harness agent will launch: the one the codex SDK is pinned
    to, or omp through its own resolver so env overrides stay honoured.
    """
    if harness is AgentBackend.CODEX:
        from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]

        return str(bundled_codex_path())
    from openscientist.agent.base import _resolve_bin

    return _resolve_bin("OPENSCIENTIST_OMP_BIN", "omp")


def _harness_cli_version(command: str) -> str | None:
    """Bare version from the first ``<command> --version`` line; None if the CLI is unusable."""
    try:
        result = subprocess.run(
            [command, "--version"], capture_output=True, text=True, timeout=3, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first_line = result.stdout.strip().split("\n", 1)[0]
    match = re.search(r"\d+\.\d+\S*", first_line)
    return match.group(0) if match else (first_line or None)


def get_version_metadata() -> dict[str, str]:
    """Get OpenScientist version metadata for reproducibility."""
    from openscientist.version import SHORT_COMMIT_LENGTH, get_commit

    metadata: dict[str, str] = {}

    commit = get_commit()
    if commit != "unknown":
        metadata["openscientist_commit"] = commit

    openscientist_build_time = os.environ.get("OPENSCIENTIST_BUILD_TIME")  # env-ok
    if openscientist_build_time and openscientist_build_time != "unknown":
        metadata["openscientist_build_time"] = openscientist_build_time

    try:
        if Path("/.dockerenv").exists():
            with open("/etc/hostname", encoding="utf-8") as f:
                container_id = f.read().strip()
                if container_id:
                    metadata["docker_container_id"] = container_id[:SHORT_COMMIT_LENGTH]
    except OSError:
        pass

    # The resolved harness driving the job, never the literal "auto". A
    # failure here also aborts agent build, so the keys stay unrecorded.
    try:
        harness = agent_class_for_provider_id(get_settings().provider.provider_id).backend
    except Exception:
        return metadata
    metadata["agent_harness"] = harness.value

    if harness is AgentBackend.CLAUDE_CODE:
        # Private modules of an unpinned SDK; omit each key if its module moves.
        try:
            from claude_agent_sdk._cli_version import __cli_version__

            metadata["claude_code_version"] = __cli_version__
            metadata["agent_harness_version"] = __cli_version__
        except Exception:
            pass

        try:
            from claude_agent_sdk._version import __version__

            metadata["claude_agent_sdk_version"] = __version__
        except Exception:
            pass
    elif version := _harness_cli_version(_harness_binary(harness)):
        metadata["agent_harness_version"] = version

    return metadata

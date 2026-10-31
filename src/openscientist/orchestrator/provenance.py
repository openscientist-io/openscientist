"""Runtime version provenance and cost records for a discovery job."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from sqlalchemy import text, update

from openscientist.agent.base import (
    AbstractAgent,
    AgentBackend,
    TokenUsage,
)
from openscientist.agent.factory import agent_class_for_provider_id
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import async_session_local
from openscientist.providers.base import Provider
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
    from openscientist.agent.omp_agent import _resolve_omp_bin

    return _resolve_omp_bin()


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


async def _persist_job_cost_record(
    job_id: str,
    tokens: TokenUsage,
    provider_name: str,
    model_name: str,
    operation_type: str = "discovery",
    iteration: int | None = None,
) -> None:
    """Write a CostRecord for ``tokens``, spent in ``operation_type`` at ``iteration``."""
    from openscientist.database.models import CostRecord
    from openscientist.providers.pricing import estimate_cost_usd

    # The pricing buckets are keyword-only, since they are easy to transpose and each
    # is priced differently.
    cost_usd = estimate_cost_usd(
        model_name,
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_read_tokens=tokens.cache_read_tokens,
        cache_write_tokens=tokens.cache_write_tokens,
        cache_write_1h_tokens=tokens.cache_write_1h_tokens,
        reasoning_tokens=tokens.reasoning_tokens,
    )
    async with async_session_local() as session:
        record = CostRecord(
            job_id=UUID(job_id),
            iteration=iteration,
            operation_type=operation_type,
            provider=provider_name,
            model=model_name,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cache_read_tokens=tokens.cache_read_tokens,
            # The column holds every cache write. Only the price depends on how long
            # the entry lives, and `cost_usd` already accounts for that.
            cache_write_tokens=tokens.cache_write_tokens + tokens.cache_write_1h_tokens,
            reasoning_tokens=tokens.reasoning_tokens,
            cost_usd=cost_usd,
        )
        session.add(record)
        await session.commit()


class _CostLedger:
    """A run's cost, written as it goes. Each record holds the tokens spent
    since the last record that was written, so a failed write is carried into
    the next one."""

    def __init__(
        self,
        job_id: str,
        *,
        usage: Callable[[], TokenUsage],
        provider_name: str,
        model_name: str,
    ) -> None:
        self._job_id = job_id
        self._usage = usage
        self._provider_name = provider_name
        self._model_name = model_name
        self._written = TokenUsage()
        self._operation_type = "discovery"
        self._iteration: int | None = None

    @classmethod
    def for_executor(cls, executor: AbstractAgent[Provider], job_id: str) -> _CostLedger:
        settings = get_settings()
        provider = executor.provider
        # A chat turn can run on its own model (model_override), which then
        # sets the price.
        model_name = (
            executor.config.model_override
            or settings.provider.model
            or settings.provider.anthropic_default_sonnet_model
            or provider.effective_model_name()
            or "unknown"
        )
        return cls(
            job_id,
            usage=lambda: executor.total_tokens,
            provider_name=provider.display_name,
            model_name=model_name,
        )

    def begin(self, operation_type: str, iteration: int | None) -> None:
        """Label the tokens spent from now on."""
        self._operation_type = operation_type
        self._iteration = iteration

    def _unwritten(self) -> tuple[TokenUsage, TokenUsage]:
        # A copy, since agents grow their running total in place.
        total = replace(self._usage())
        return total, total - self._written

    async def record(self) -> bool:
        """Write the tokens spent since the last written record. Returns whether
        nothing is left unwritten."""
        total, unwritten = self._unwritten()
        if unwritten == TokenUsage():
            return True
        try:
            await _persist_job_cost_record(
                self._job_id,
                unwritten,
                self._provider_name,
                self._model_name,
                operation_type=self._operation_type,
                iteration=self._iteration,
            )
        except Exception as e:
            logger.warning(
                "Could not write the cost of job %s, carrying it to the next record: %s",
                self._job_id,
                e,
            )
            return False
        self._written = total
        return True

    async def close(self) -> None:
        """Write what is left. If that fails too, log every count so the
        tokens can still be billed."""
        if await self.record():
            return
        _, unwritten = self._unwritten()
        logger.error(
            "Cost of job %s was not saved: provider=%s model=%s input=%d output=%d "
            "cache_write=%d cache_write_1h=%d cache_read=%d reasoning=%d",
            self._job_id,
            self._provider_name,
            self._model_name,
            unwritten.input_tokens,
            unwritten.output_tokens,
            unwritten.cache_write_tokens,
            unwritten.cache_write_1h_tokens,
            unwritten.cache_read_tokens,
            unwritten.reasoning_tokens,
        )

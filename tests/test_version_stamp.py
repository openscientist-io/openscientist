"""Recording version provenance must never end a discovery run.

A run once failed on this write after iteration 1, and the column shows the
write had in fact succeeded: the deadline fired, the exception escaped, and a
multi-hour run died over two version strings.
"""

import asyncio
import logging
import time
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from openscientist.database.models import Job
from openscientist.database.session import AsyncSessionLocal
from openscientist.orchestrator.discovery import _sync_version_metadata

_VERSION_INFO = {"agent_harness": "omp", "agent_harness_version": "17.1.5"}


async def _create_job(job_id: UUID) -> None:
    """Commit a job row on its own connection, as the orchestrator sees one."""
    async with AsyncSessionLocal(thread_safe=True) as session:
        session.add(Job(id=job_id, research_question="Version stamp", status="running"))
        await session.commit()


async def _delete_job(job_id: UUID) -> None:
    async with AsyncSessionLocal(thread_safe=True) as session:
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


async def _stored_version_info(job_id: UUID) -> dict[str, str] | None:
    async with AsyncSessionLocal(thread_safe=True) as session:
        result = await session.execute(select(Job.version_info).where(Job.id == job_id))
        return result.scalar_one_or_none()


@pytest.mark.asyncio
async def test_the_stamp_is_written(_apply_migrations_once: None) -> None:
    job_id = uuid4()
    await _create_job(job_id)
    try:
        with patch(
            "openscientist.orchestrator.discovery.get_version_metadata",
            return_value=_VERSION_INFO,
        ):
            await _sync_version_metadata(str(job_id))
        assert await _stored_version_info(job_id) == _VERSION_INFO
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_a_failed_stamp_does_not_end_the_run(
    _apply_migrations_once: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The write is provenance, not results. A database that will not take it
    must cost the run its version strings, nothing more."""
    with (
        patch(
            "openscientist.orchestrator.discovery.get_version_metadata",
            return_value=_VERSION_INFO,
        ),
        patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            side_effect=TimeoutError("statement timeout"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        await _sync_version_metadata(str(uuid4()))

    assert "version" in caplog.text.lower()


class _HangingSession:
    """A session that never finishes connecting, which is what a stalled host
    or connection pool looks like from here."""

    async def __aenter__(self) -> "_HangingSession":
        await asyncio.sleep(3600)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_a_hanging_stamp_gives_up_at_its_deadline(
    _apply_migrations_once: None, caplog: pytest.LogCaptureFixture
) -> None:
    """``statement_timeout`` only bounds a statement the database has started,
    so it cannot cover name resolution, connecting, or waiting for a
    connection. The attempt carries its own deadline for those."""
    with (
        patch(
            "openscientist.orchestrator.discovery.get_version_metadata",
            return_value=_VERSION_INFO,
        ),
        patch(
            "openscientist.orchestrator.discovery.AsyncSessionLocal",
            return_value=_HangingSession(),
        ),
        patch("openscientist.orchestrator.discovery._STAMP_DEADLINE_SECONDS", 0.2),
        caplog.at_level(logging.WARNING),
    ):
        started = time.monotonic()
        await _sync_version_metadata(str(uuid4()))

    assert time.monotonic() - started < 5
    assert "version" in caplog.text.lower()


@pytest.mark.asyncio
async def test_nothing_is_written_when_there_is_no_metadata(_apply_migrations_once: None) -> None:
    job_id = uuid4()
    await _create_job(job_id)
    try:
        with patch("openscientist.orchestrator.discovery.get_version_metadata", return_value={}):
            await _sync_version_metadata(str(job_id))
        assert await _stored_version_info(job_id) is None
    finally:
        await _delete_job(job_id)

"""The web's system paths (worker threads, notifications) act without a user and
must not depend on the web login's own rights: they run through the admin login.

Each test logs in as the real web login, which reads no table by itself, so a
path that still used it fails here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job, User
from openscientist.database.session import thread_safe_admin_session
from openscientist.job.repository import (
    delete_job,
    get_job,
    get_notification_target,
    list_jobs,
    set_job_status,
)
from openscientist.job.types import JobStatus
from openscientist.job_manager import _load_progress_from_knowledge_state
from openscientist.knowledge_state import KnowledgeState
from openscientist.ntfy import ensure_user_has_topic, get_user_ntfy_settings


@dataclass(frozen=True)
class _OwnedJob:
    job_id: str
    owner_id: UUID


@pytest_asyncio.fixture
async def owned_job(test_engine: AsyncEngine) -> AsyncGenerator[_OwnedJob, None]:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        owner = User(email=f"owner-{uuid4().hex}@system.test", name="Owner")
        session.add(owner)
        await session.flush()
        job = Job(owner_id=owner.id, research_question="System paths", status="queued")
        session.add(job)
        await session.commit()
    yield _OwnedJob(job_id=str(job.id), owner_id=owner.id)
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job.id})
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": owner.id})


async def _status(test_engine: AsyncEngine, job_id: str) -> str | None:
    async with test_engine.connect() as conn:
        result = await conn.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id})
        value = result.scalar_one_or_none()
        return None if value is None else str(value)


@pytest.mark.asyncio
async def test_worker_paths_reach_a_users_job_without_the_web_login(
    web_login: None, owned_job: _OwnedJob, test_engine: AsyncEngine
) -> None:
    queued = [job.id for job in await list_jobs(status=JobStatus.QUEUED)]
    assert UUID(owned_job.job_id) in queued

    target = await set_job_status(
        owned_job.job_id, JobStatus.RUNNING, allowed_from={JobStatus.QUEUED}
    )
    assert target.owner_id == str(owned_job.owner_id)
    assert await _status(test_engine, owned_job.job_id) == "running"

    job = await get_job(owned_job.job_id)
    assert job is not None and job.status == "running"
    assert (await get_notification_target(owned_job.job_id)).owner_id == str(owned_job.owner_id)


@pytest.mark.asyncio
async def test_a_job_is_deleted_without_a_user_or_the_web_login(
    web_login: None, owned_job: _OwnedJob, test_engine: AsyncEngine
) -> None:
    await delete_job(owned_job.job_id)
    assert await _status(test_engine, owned_job.job_id) is None


@pytest.mark.asyncio
async def test_notification_lookups_work_without_the_web_login(
    web_login: None, owned_job: _OwnedJob, test_engine: AsyncEngine
) -> None:
    assert await get_user_ntfy_settings(owned_job.owner_id) == (True, None)
    topic = await ensure_user_has_topic(owned_job.owner_id)
    assert topic is not None
    async with test_engine.connect() as conn:
        stored = await conn.execute(
            text("SELECT ntfy_topic FROM users WHERE id = :id"), {"id": owned_job.owner_id}
        )
        assert stored.scalar_one() == topic


def test_job_progress_is_read_without_the_web_login(web_login: None, owned_job: _OwnedJob) -> None:
    """A failed progress read returns defaults, so a real finding count proves the read worked."""
    ks = KnowledgeState(owned_job.job_id, "System paths", 3)
    ks.add_finding("A finding", "evidence")
    ks.save_to_database_sync(owned_job.job_id, open_session=thread_safe_admin_session)
    assert _load_progress_from_knowledge_state(owned_job.job_id, "completed", -1, -1) == (1, 1)

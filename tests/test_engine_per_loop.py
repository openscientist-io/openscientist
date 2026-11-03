"""Sessions reuse a pooled engine of their own loop (plans/engine-per-loop.md)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from functools import partial
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.async_tasks import run_sync, thread_loop
from openscientist.database.models import User
from openscientist.database.session import (
    async_session_local,
    get_admin_session,
    thread_safe_admin_session,
    thread_safe_user_session,
)
from openscientist.job_manager import JobManager

Opener = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@pytest_asyncio.fixture
async def user_id(test_engine: AsyncEngine) -> UUID:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        user = User(email=f"pool-{uuid4().hex}@engine.test", name="Pooled")
        session.add(user)
        await session.commit()
    return user.id


async def _backend_pid(open_session: Opener) -> int:
    async with open_session() as session:
        return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


def _openers(user_id: UUID) -> dict[str, Opener]:
    return {
        "web login": async_session_local,
        "admin": thread_safe_admin_session,
        "admin context": get_admin_session,
        "user": partial(thread_safe_user_session, user_id),
    }


@pytest.mark.parametrize("kind", ["web login", "admin", "admin context", "user"])
async def test_sessions_on_one_loop_reuse_a_connection(user_id: UUID, kind: str) -> None:
    open_session = _openers(user_id)[kind]
    assert await _backend_pid(open_session) == await _backend_pid(open_session)


async def test_a_reused_connection_carries_no_previous_user_or_role(user_id: UUID) -> None:
    async with thread_safe_user_session(user_id) as session:
        first = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    async with async_session_local() as session:
        row = (
            await session.execute(
                text(
                    "SELECT pg_backend_pid(), current_user = session_user,"
                    " coalesce(current_setting('app.current_user_id', true), '')"
                )
            )
        ).one()
    assert row[0] == first
    assert row[1] is True
    assert row[2] == ""


async def _pid_in_new_session() -> int:
    return await _backend_pid(thread_safe_admin_session)


async def _is_connected(test_engine: AsyncEngine, pid: int) -> bool:
    async with test_engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
        )
    return bool(found)


async def test_a_run_sync_call_closes_its_connection_when_its_loop_ends(
    test_engine: AsyncEngine,
) -> None:
    pids: list[int] = []
    worker = threading.Thread(target=lambda: pids.append(run_sync(_pid_in_new_session())))
    worker.start()
    worker.join(timeout=30)
    assert pids
    assert not await _is_connected(test_engine, pids[0])


async def test_a_worker_loop_and_this_loop_use_separate_connections(user_id: UUID) -> None:
    async with thread_safe_admin_session() as held:
        mine = (await held.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        pids: list[int] = []
        worker = threading.Thread(target=lambda: pids.append(run_sync(_pid_in_new_session())))
        worker.start()
        worker.join(timeout=30)
        assert pids
        assert pids[0] != mine
        assert (await held.execute(text("SELECT 1"))).scalar_one() == 1


async def test_run_sync_calls_in_a_thread_loop_share_a_connection_until_it_ends(
    test_engine: AsyncEngine,
) -> None:
    def job_thread() -> list[int]:
        with thread_loop():
            return [run_sync(_pid_in_new_session()) for _ in range(2)]

    pids = await asyncio.wait_for(asyncio.to_thread(job_thread), timeout=30)
    assert pids[0] == pids[1]
    assert not await _is_connected(test_engine, pids[0])


async def test_a_job_shares_one_pooled_connection_until_it_finishes(
    test_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    manager = JobManager(jobs_dir=tmp_path)
    pids: list[int] = []

    def container(_job_id: str, run_mode: str = "discovery") -> None:
        pids.extend(run_sync(_pid_in_new_session()) for _ in range(2))

    # Finishing a job starts the next queued one, which would be another test's.
    with (
        patch.object(manager, "_run_job_in_container", side_effect=container),
        patch.object(manager, "_start_next_queued_job"),
    ):
        await asyncio.wait_for(asyncio.to_thread(manager._run_job, "job-1"), timeout=30)

    assert len(pids) == 2
    assert pids[0] == pids[1]
    assert not await _is_connected(test_engine, pids[0])

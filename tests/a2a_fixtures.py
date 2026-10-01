"""Shared PostgreSQL-backed A2A fixtures for API and UI tests."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openscientist.api import a2a, auth
from openscientist.api.endpoints import jobs
from openscientist.database.models import A2ASettings, Administrator, APIKey, Job, User
from openscientist.database.session import get_session
from openscientist.job_manager import JobManager
from openscientist.settings import clear_settings_cache


@dataclass
class Environment:
    client: AsyncClient
    factory: async_sessionmaker[AsyncSession]
    manager: JobManager
    users: list[User]
    tokens: list[str]
    started: list[str]
    app_session: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    admin_session: Callable[[], AbstractAsyncContextManager[AsyncSession]]

    def use_user(self, index: int) -> None:
        self.client.headers["Authorization"] = f"Bearer {self.tokens[index]}"

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.client.post(
            "/a2a", json={"jsonrpc": "2.0", "id": "test", "method": method, "params": params or {}}
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    async def state(self, task_id: str, state: str) -> None:
        async with self.factory() as session:
            job = await session.get(Job, UUID(task_id))
            assert job is not None
            job.status = state
            await session.commit()


@pytest_asyncio.fixture
async def environment(
    test_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Environment]:
    """Use the real manager and DB; replace only budget lookup and execution."""
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSCIENTIST_MODEL", "claude-test-model")
    monkeypatch.setenv("APP_URL", "https://example.org/scientist")
    clear_settings_cache()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def admin_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    @asynccontextmanager
    async def app_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            await session.execute(text("SET ROLE openscientist_app"))
            try:
                yield session
            finally:
                await session.rollback()
                await session.execute(text("RESET ROLE"))
                await session.commit()

    monkeypatch.setattr(a2a, "get_session_ctx", app_session)
    monkeypatch.setattr(a2a, "get_admin_session", admin_session)
    monkeypatch.setattr(auth, "get_admin_session", admin_session)
    users = [
        User(
            email=f"a2a-{uuid4()}@example.org",
            name="A2A Test",
            is_approved=i != 2,
            ntfy_enabled=False,
        )
        for i in range(3)
    ]
    tokens = []
    async with factory() as session:
        session.add_all(users)
        await session.flush()
        session.add(Administrator(user_id=users[0].id))
        for user in users:
            secret = uuid4().hex
            session.add(APIKey(user_id=user.id, name="client", key_hash=auth.hash_secret(secret)))
            tokens.append(f"client:{secret}")
        setting = await session.get(A2ASettings, 1)
        assert setting is not None
        setting.enabled = True
        await session.commit()

    manager = await asyncio.to_thread(JobManager, jobs_dir=tmp_path)
    started: list[str] = []
    monkeypatch.setattr(manager, "_check_budget_before_creation", lambda: None)
    monkeypatch.setattr(manager, "start_job", started.append)
    monkeypatch.setattr(manager, "_start_next_queued_job", lambda: None)
    monkeypatch.setattr(a2a, "_get_job_manager", lambda: manager)
    monkeypatch.setattr(jobs, "_get_job_manager", lambda: manager)
    app = FastAPI()

    async def rest_session() -> AsyncIterator[AsyncSession]:
        async with app_session() as session:
            yield session

    app.dependency_overrides[get_session] = rest_session
    app.include_router(a2a.router)
    app.include_router(jobs.router, prefix="/api/v1")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            env = Environment(
                client, factory, manager, users, tokens, started, app_session, admin_session
            )
            env.use_user(0)
            yield env
    finally:
        async with factory() as session:
            await session.execute(delete(Job).where(Job.owner_id.in_([u.id for u in users])))
            await session.execute(delete(User).where(User.id.in_([u.id for u in users])))
            setting = await session.get(A2ASettings, 1)
            assert setting is not None
            setting.enabled = True
            await session.commit()
        clear_settings_cache()

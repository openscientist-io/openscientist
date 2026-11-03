"""Per-container database logins, against the real test database, created and
dropped by the web's own login as in production."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job
from openscientist.job_container import db_login
from openscientist.job_container.db_login import (
    create_agent_login,
    drop_agent_login,
    reap_agent_logins,
)
from openscientist.job_container.secrets import (
    broker_accepts,
    issue_run_credentials,
    proxy_accepts,
)


async def _connect(database_url: str) -> asyncpg.Connection:
    url = make_url(database_url)
    return await asyncpg.connect(
        host=url.host,
        port=url.port,
        database=url.database,
        user=url.username,
        password=url.password,
    )


@pytest.fixture(autouse=True)
def _as_the_web_login(web_login: None) -> None:
    pass


@pytest_asyncio.fixture
async def job_id(test_engine: AsyncEngine) -> AsyncGenerator[UUID, None]:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        job = Job(owner_id=None, research_question="Login lifecycle")
        session.add(job)
        await session.commit()
    yield job.id
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job.id})


@pytest.mark.asyncio
async def test_a_failed_login_creation_does_not_reveal_the_password(
    job_id: UUID, test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The role name is taken, so ``CREATE ROLE`` fails, and its error carries
    the statement the database rejected."""
    taken = "0123456789abcdef0123456789abcdef"
    password = "the-agent-password-nobody-may-see"
    monkeypatch.setattr(db_login.secrets, "token_hex", lambda _n: taken)
    monkeypatch.setattr(db_login.secrets, "token_urlsafe", lambda _n: password)
    async with test_engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE agent_{taken}"))
    try:
        with pytest.raises(Exception) as failure:
            await create_agent_login(str(job_id), lifetime_seconds=300)
        assert f"agent_{taken}" in str(failure.value)
        assert password not in str(failure.value)
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text(f"DROP ROLE agent_{taken}"))


@pytest.mark.asyncio
async def test_a_login_reaches_its_job_until_it_is_dropped(
    job_id: UUID, test_engine: AsyncEngine
) -> None:
    login = await create_agent_login(str(job_id), lifetime_seconds=300)
    conn = await _connect(login.database_url)
    try:
        assert {row["id"] for row in await conn.fetch("SELECT id FROM jobs")} == {job_id}
    finally:
        await conn.close()
    async with test_engine.connect() as engine_conn:
        valid_until = (
            await engine_conn.execute(
                text("SELECT rolvaliduntil FROM pg_roles WHERE rolname = :role"),
                {"role": login.role},
            )
        ).scalar_one()
    # Valid for the container's lifetime plus a short grace, and no longer.
    assert datetime.now(UTC) + timedelta(minutes=5) < valid_until
    assert valid_until < datetime.now(UTC) + timedelta(minutes=20)

    await drop_agent_login(login.role)

    with pytest.raises(asyncpg.exceptions.InvalidAuthorizationSpecificationError):
        await _connect(login.database_url)
    async with test_engine.connect() as engine_conn:
        mapped = (
            await engine_conn.execute(
                text("SELECT count(*) FROM agent_job_roles WHERE role_name = :role"),
                {"role": login.role},
            )
        ).scalar_one()
    assert mapped == 0


@pytest.mark.asyncio
async def test_dropping_a_login_ends_its_open_sessions(job_id: UUID) -> None:
    login = await create_agent_login(str(job_id), lifetime_seconds=300)
    conn = await _connect(login.database_url)
    try:
        await drop_agent_login(login.role)
        with pytest.raises(
            (
                asyncpg.exceptions.AdminShutdownError,
                asyncpg.exceptions.ConnectionDoesNotExistError,
                asyncpg.exceptions.InterfaceError,
            )
        ):
            await conn.fetch("SELECT id FROM jobs")
    finally:
        if not conn.is_closed():
            conn.terminate()


@pytest.mark.asyncio
async def test_dropping_a_login_ends_its_runs_proxy_and_broker_credentials(job_id: UUID) -> None:
    login = await create_agent_login(str(job_id), lifetime_seconds=300)
    credentials = issue_run_credentials(str(job_id), login.role, "discovery")
    await drop_agent_login(login.role)
    assert not proxy_accepts(credentials.llm_placeholder)
    assert not broker_accepts(str(job_id), credentials.exec_token)


@pytest.mark.asyncio
async def test_only_agent_logins_can_be_dropped() -> None:
    with pytest.raises(ValueError, match="not an agent login"):
        await drop_agent_login("openscientist_admin")


async def _exists(test_engine: AsyncEngine, role: str) -> bool:
    async with test_engine.connect() as conn:
        found = await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
        return found.first() is not None


async def _age_mapping(test_engine: AsyncEngine, role: str, minutes: int) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE agent_job_roles SET created_at = now() - make_interval(mins => :m)"
                " WHERE role_name = :r"
            ),
            {"m": minutes, "r": role},
        )


async def _expire(test_engine: AsyncEngine, role: str) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE {role} VALID UNTIL '2000-01-01'"))


@pytest_asyncio.fixture
async def make_login(job_id: UUID) -> AsyncGenerator[Callable[[], Awaitable[str]], None]:
    """Creates logins for ``job_id`` and drops whatever the test left."""
    created: list[str] = []

    async def _make() -> str:
        login = await create_agent_login(str(job_id), lifetime_seconds=300)
        created.append(login.role)
        return login.role

    yield _make
    for role in created:
        await drop_agent_login(role)


@pytest.mark.asyncio
async def test_the_reaper_keeps_logins_whose_container_runs_or_is_starting(
    make_login: Callable[[], Awaitable[str]], test_engine: AsyncEngine
) -> None:
    running, starting = await make_login(), await make_login()
    dropped = await reap_agent_logins({running})
    assert {running, starting}.isdisjoint(dropped)
    assert await _exists(test_engine, running)
    assert await _exists(test_engine, starting)


@pytest.mark.asyncio
async def test_the_reaper_drops_a_login_whose_container_never_appeared(
    make_login: Callable[[], Awaitable[str]], test_engine: AsyncEngine
) -> None:
    orphan = await make_login()
    await _age_mapping(test_engine, orphan, minutes=16)
    assert orphan in await reap_agent_logins(set())
    assert not await _exists(test_engine, orphan)


@pytest.mark.asyncio
async def test_the_reaper_drops_an_expired_login_even_while_its_container_runs(
    make_login: Callable[[], Awaitable[str]], test_engine: AsyncEngine
) -> None:
    """Open sessions outlive VALID UNTIL, so expiry alone does not end them."""
    expired = await make_login()
    await _expire(test_engine, expired)
    assert expired in await reap_agent_logins({expired})
    assert not await _exists(test_engine, expired)


@pytest.mark.asyncio
async def test_the_reaper_drops_a_login_with_no_mapping(
    make_login: Callable[[], Awaitable[str]], test_engine: AsyncEngine
) -> None:
    unmapped = await make_login()
    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM agent_job_roles WHERE role_name = :r"), {"r": unmapped}
        )
    assert unmapped in await reap_agent_logins({unmapped})
    assert not await _exists(test_engine, unmapped)


@pytest.mark.asyncio
async def test_without_docker_the_reaper_only_drops_what_needs_no_container_list(
    make_login: Callable[[], Awaitable[str]], test_engine: AsyncEngine
) -> None:
    old, expired = await make_login(), await make_login()
    await _age_mapping(test_engine, old, minutes=60)
    await _expire(test_engine, expired)
    dropped = await reap_agent_logins(None)
    assert expired in dropped
    assert old not in dropped
    assert await _exists(test_engine, old)


@pytest.mark.asyncio
async def test_the_reaper_never_touches_roles_outside_openscientist_agent(
    test_engine: AsyncEngine,
) -> None:
    lookalike = "agent_" + "f" * 32
    async with test_engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {lookalike} LOGIN VALID UNTIL '2000-01-01'"))
    try:
        assert lookalike not in await reap_agent_logins(set())
        assert await _exists(test_engine, lookalike)
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text(f"DROP ROLE {lookalike}"))

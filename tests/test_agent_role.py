"""A job's database login, a member of ``openscientist_agent``, reaches only the
job it is mapped to in ``agent_job_roles``."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.agent.base import TokenUsage
from openscientist.database.models import CostRecord, Job, JobDataFile, User
from openscientist.knowledge_state import KnowledgeState
from openscientist.settings import get_settings

_LOGIN_PASSWORD = "agent-login-test-password"

# Every job-scoped table the agent container reads or writes.
_JOB_TABLES = (
    "hypotheses",
    "findings",
    "literature",
    "analysis_log",
    "iteration_summaries",
    "feedback_history",
    "job_data_files",
    "cost_records",
)


@dataclass(frozen=True)
class _Jobs:
    own: UUID
    other: UUID
    orphan: UUID
    other_owner: UUID


async def _fill_knowledge(job_id: UUID, research_question: str) -> None:
    ks = KnowledgeState(str(job_id), research_question, 3)
    ks.add_hypothesis(f"{research_question} hypothesis")
    ks.add_finding(f"{research_question} finding", "evidence")
    ks.add_literature("123", f"{research_question} paper", "abstract")
    ks.log_analysis("analysis", code="print(1)", output="1")
    ks.add_iteration_summary(1, f"{research_question} summary")
    ks.add_feedback(f"{research_question} feedback", 1)
    await ks.save_to_database(str(job_id))


@pytest_asyncio.fixture
async def jobs(test_engine: AsyncEngine) -> AsyncGenerator[_Jobs, None]:
    """Job ``own`` and another user's job ``other``, both with a row in every
    job table, plus an ownerless job. Committed, so other logins see them."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        alice = User(email=f"alice-{uuid4().hex}@agent.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@agent.test", name="Bob")
        session.add_all([alice, bob])
        await session.flush()
        own = Job(owner_id=alice.id, research_question="Own")
        other = Job(owner_id=bob.id, research_question="Other")
        orphan = Job(owner_id=None, research_question="Orphan")
        session.add_all([own, other, orphan])
        await session.flush()
        for job in (own, other):
            session.add(
                JobDataFile(
                    job_id=job.id,
                    filename="data.csv",
                    file_path=f"/jobs/{job.id}/data.csv",
                    file_type="csv",
                    file_size=1,
                )
            )
        session.add(
            CostRecord(
                job_id=other.id,
                operation_type="discovery",
                provider="vllm",
                model="m",
                input_tokens=1,
                output_tokens=1,
                cache_read_tokens=0,
                cache_write_tokens=0,
                reasoning_tokens=0,
                cost_usd=0.0,
            )
        )
        await session.commit()
        ids = _Jobs(own=own.id, other=other.id, orphan=orphan.id, other_owner=bob.id)
        user_ids = [alice.id, bob.id]
    await _fill_knowledge(ids.other, "Other")
    yield ids
    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM jobs WHERE id = ANY(:ids)"),
            {"ids": [ids.own, ids.other, ids.orphan]},
        )
        await conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": user_ids})


@asynccontextmanager
async def _job_login(
    test_engine: AsyncEngine, test_database_url: str, job_id: UUID
) -> AsyncGenerator[str, None]:
    """A login for one container of ``job_id``, as the launcher will create it."""
    role = f"agent_{uuid4().hex}"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{_LOGIN_PASSWORD}' IN ROLE openscientist_agent"
            )
        )
        await conn.execute(
            text("INSERT INTO agent_job_roles (role_name, job_id) VALUES (:role, :job)"),
            {"role": role, "job": job_id},
        )
    try:
        url = make_url(test_database_url).set(username=role, password=_LOGIN_PASSWORD)
        yield url.render_as_string(hide_password=False)
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM agent_job_roles WHERE role_name = :role"), {"role": role}
            )
            await conn.execute(text(f"DROP ROLE {role}"))


async def _connect(login_url: str) -> asyncpg.Connection:
    url = make_url(login_url)
    return await asyncpg.connect(
        host=url.host,
        port=url.port,
        database=url.database,
        user=url.username,
        password=url.password,
    )


@pytest_asyncio.fixture
async def login_url(
    test_engine: AsyncEngine, test_database_url: str, jobs: _Jobs
) -> AsyncGenerator[str, None]:
    async with _job_login(test_engine, test_database_url, jobs.own) as url:
        yield url


@pytest_asyncio.fixture
async def login(login_url: str) -> AsyncGenerator[asyncpg.Connection, None]:
    conn = await _connect(login_url)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_a_job_can_have_a_login_per_container(
    login: asyncpg.Connection, jobs: _Jobs, test_engine: AsyncEngine, test_database_url: str
) -> None:
    """A chat turn's container runs beside the discovery container of the same
    job, each under its own login, and both reach that job."""
    async with _job_login(test_engine, test_database_url, jobs.own) as chat_url:
        chat = await _connect(chat_url)
        try:
            assert {row["id"] for row in await chat.fetch("SELECT id FROM jobs")} == {jobs.own}
        finally:
            await chat.close()
    assert {row["id"] for row in await login.fetch("SELECT id FROM jobs")} == {jobs.own}


@pytest.fixture
def container_database(login_url: str) -> object:
    """Run the container's own code with DATABASE_URL set to the job login."""
    saved = os.environ.get("DATABASE_URL")

    os.environ["DATABASE_URL"] = login_url
    get_settings.cache_clear()
    yield None
    if saved is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = saved
    get_settings.cache_clear()


async def _owners_count(test_engine: AsyncEngine, table: str, job_id: UUID) -> int:
    async with test_engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE job_id = :job"), {"job": job_id}
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_login_sees_only_its_own_job(
    login: asyncpg.Connection, jobs: _Jobs, test_engine: AsyncEngine
) -> None:
    assert {row["id"] for row in await login.fetch("SELECT id FROM jobs")} == {jobs.own}
    for table in _JOB_TABLES:
        assert await _owners_count(test_engine, table, jobs.other) > 0, table
        visible = {row["job_id"] for row in await login.fetch(f"SELECT job_id FROM {table}")}
        assert visible <= {jobs.own}, table


@pytest.mark.asyncio
async def test_a_later_policy_for_every_role_cannot_widen_the_login(
    login: asyncpg.Connection, jobs: _Jobs, test_engine: AsyncEngine
) -> None:
    """A policy written later without a TO clause applies to every role. The
    restrictive agent policy still holds a job login to its own rows."""
    async with test_engine.begin() as conn:
        await conn.execute(
            text("CREATE POLICY findings_probe_all ON findings FOR SELECT USING (true)")
        )
    try:
        visible = {row["job_id"] for row in await login.fetch("SELECT job_id FROM findings")}
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text("DROP POLICY findings_probe_all ON findings"))
    assert jobs.other not in visible


@pytest.mark.asyncio
async def test_a_spoofed_user_setting_reaches_nothing_more(
    login: asyncpg.Connection, jobs: _Jobs
) -> None:
    """The web's policies trust app.current_user_id, which any login can set for
    itself. A job login that sets it to another user still sees only its job."""
    await login.execute(
        "SELECT set_config('app.current_user_id', $1, false)", str(jobs.other_owner)
    )
    assert {row["id"] for row in await login.fetch("SELECT id FROM jobs")} == {jobs.own}
    assert await login.fetch("SELECT id FROM findings WHERE job_id = $1", jobs.other) == []
    updated = await login.execute("UPDATE jobs SET status = 'failed' WHERE id = $1", jobs.other)
    assert updated == "UPDATE 0"


@pytest.mark.asyncio
async def test_login_cannot_write_into_another_job(login: asyncpg.Connection, jobs: _Jobs) -> None:
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await login.execute(
            "INSERT INTO findings (job_id, iteration, text, finding_type, source, data)"
            " VALUES ($1, 1, 'planted', 'observation', 'code_execution', '{}')",
            jobs.other,
        )


@pytest.mark.asyncio
async def test_login_cannot_change_who_owns_its_job(login: asyncpg.Connection, jobs: _Jobs) -> None:
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await login.execute(
            "UPDATE jobs SET owner_id = $1 WHERE id = $2", jobs.other_owner, jobs.own
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table", ["users", "api_keys", "sessions", "job_shares", "administrators", "agent_job_roles"]
)
async def test_login_cannot_read_tables_outside_the_job(
    login: asyncpg.Connection, table: str
) -> None:
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await login.fetch(f"SELECT 1 FROM {table} LIMIT 1")


@pytest.mark.asyncio
async def test_container_code_runs_as_the_job_login(
    jobs: _Jobs,
    test_engine: AsyncEngine,
    container_database: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container's own reads and writes, run through the job login, so a
    grant the code needs and the migration lacks fails here."""
    from openscientist.orchestrator.discovery import (
        _persist_job_cost_record,
        _sync_version_metadata,
    )
    from openscientist.orchestrator.iteration import update_job_status

    await _fill_knowledge(jobs.own, "Own")
    ks = await KnowledgeState.load_from_database(str(jobs.own))
    assert [f["title"] for f in ks.data["findings"]] == ["Own finding"]
    hypothesis_id = ks.data["hypotheses"][0]["id"]
    ks.update_hypothesis(hypothesis_id, {"status": "supported"})
    ks.add_finding("Own finding", "more evidence")
    ks.data["literature"][0]["abstract"] = "revised abstract"
    ks.add_iteration_summary(1, "revised summary")
    ks.set_agent_status("Working")
    await ks.save_to_database(str(jobs.own))

    await update_job_status(tmp_path / str(jobs.own), "running", allowed_from=("pending",))
    await _persist_job_cost_record(str(jobs.own), TokenUsage(input_tokens=5), "vllm", "m")
    with patch(
        "openscientist.orchestrator.discovery.get_version_metadata",
        return_value={"agent_harness": "omp"},
    ):
        await _sync_version_metadata(str(jobs.own))

    os.environ.setdefault("OPENSCIENTIST_JOB_ID", str(jobs.own))
    os.environ.setdefault("OPENSCIENTIST_JOB_DIR", str(tmp_path))
    from openscientist_tools.job_meta import set_job_title
    from openscientist_tools.state import STATE

    monkeypatch.setattr(STATE, "job_id", str(jobs.own))
    assert (await set_job_title("Own title")).startswith("✅")

    async with test_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, short_title, version_info, agent_status FROM jobs WHERE id = :id"
                ),
                {"id": jobs.own},
            )
        ).one()
        statuses = (
            await conn.execute(
                text("SELECT status FROM hypotheses WHERE job_id = :id"), {"id": jobs.own}
            )
        ).scalars()
        assert list(statuses) == ["supported"]
        abstracts = (
            await conn.execute(
                text("SELECT abstract FROM literature WHERE job_id = :id"), {"id": jobs.own}
            )
        ).scalars()
        assert list(abstracts) == ["revised abstract"]
        summaries = (
            await conn.execute(
                text("SELECT summary_text FROM iteration_summaries WHERE job_id = :id"),
                {"id": jobs.own},
            )
        ).scalars()
        assert list(summaries) == ["revised summary"]
    assert row.status == "running"
    assert row.short_title == "Own title"
    assert row.version_info == {"agent_harness": "omp"}
    assert row.agent_status == "Working"
    assert await _owners_count(test_engine, "cost_records", jobs.own) == 1


@pytest.mark.asyncio
async def test_the_job_login_records_when_its_job_ends(
    jobs: _Jobs, test_engine: AsyncEngine, container_database: object, tmp_path: Path
) -> None:
    """The scheduler weighs owners by slot-hours, so the container's final write
    must also record the end of the run, and the job login may clear the label."""
    from openscientist.orchestrator.iteration import update_job_status

    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET status = 'running', started_at = now(), "
                "agent_status = 'Working', agent_status_updated_at = now() WHERE id = :id"
            ),
            {"id": jobs.own},
        )
    assert await update_job_status(tmp_path / str(jobs.own), "completed", allowed_from=("running",))
    async with test_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT ended_at, agent_status FROM jobs WHERE id = :id"), {"id": jobs.own}
            )
        ).one()
    assert row.ended_at is not None
    assert row.agent_status is None

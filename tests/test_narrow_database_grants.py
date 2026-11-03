"""The web's roles touch only what the web legitimately changes.

An ``edit`` share updates the run columns of the shared job, never the owner
fields. A ``view`` share reads the junction rows and writes nothing. The
SECURITY DEFINER helpers are closed to every login that does not call them,
and no web login may TRUNCATE.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import (
    Administrator,
    APIKey,
    Finding,
    Hypothesis,
    Job,
    JobShare,
    Literature,
    User,
)
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls

_LOGIN_PASSWORD = "narrow-grants-test-password"

# SQL assignments for every jobs column the app role must lose. The values are
# valid against the column constraints, so only the grant can stop the write.
_PROTECTED_ASSIGNMENTS: tuple[tuple[str, str], ...] = (
    ("owner_id", ":stranger"),
    ("id", ":stranger"),
    ("research_question", "'planted'"),
    ("short_title", "'planted'"),
    ("description", "'planted'"),
    ("use_hypotheses", "true"),
    ("investigation_mode", "'coinvestigate'"),
    ("max_iterations", "9"),
    ("pdb_code", "'1ABC'"),
    ("space_group", "'P2121'"),
    ("llm_provider", "'anthropic'"),
    ("llm_config", '\'{"model": "other"}\'::jsonb'),
    ("result_summary", "'planted'"),
    ("created_at", "now() - interval '1 day'"),
)

# The helper a login may not call, with the number of arguments it takes.
_HELPER_QUERIES = (
    ("SELECT is_admin($1)", 1),
    ("SELECT current_user_is_admin()", 0),
    ("SELECT check_job_owner($1, $2)", 2),
)


@dataclass(frozen=True)
class _Tenants:
    owner: UUID
    editor: UUID
    viewer: UUID
    stranger: UUID
    admin: UUID
    job: UUID
    hypothesis: UUID
    second_hypothesis: UUID
    finding: UUID
    second_finding: UUID
    literature: UUID
    second_literature: UUID


@pytest_asyncio.fixture
async def tenants(test_engine: AsyncEngine) -> AsyncGenerator[_Tenants, None]:
    """An owner's awaiting-feedback job, its edit and view shares, and one
    row in every junction table, committed for the other logins."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        owner = User(email=f"owner-{uuid4().hex}@grants.test", name="Owner")
        editor = User(email=f"editor-{uuid4().hex}@grants.test", name="Editor")
        viewer = User(email=f"viewer-{uuid4().hex}@grants.test", name="Viewer")
        stranger = User(email=f"stranger-{uuid4().hex}@grants.test", name="Stranger")
        admin = User(email=f"admin-{uuid4().hex}@grants.test", name="Admin")
        session.add_all([owner, editor, viewer, stranger, admin])
        await session.flush()
        job = Job(
            owner_id=owner.id,
            research_question="Shared",
            status="awaiting_feedback",
        )
        session.add(job)
        await session.flush()
        hypothesis = Hypothesis(job_id=job.id, iteration=1, text="first")
        second_hypothesis = Hypothesis(job_id=job.id, iteration=1, text="second")
        finding = Finding(
            job_id=job.id,
            iteration=1,
            text="first",
            finding_type="observation",
            source="code_execution",
        )
        second_finding = Finding(
            job_id=job.id,
            iteration=1,
            text="second",
            finding_type="observation",
            source="code_execution",
        )
        literature = Literature(job_id=job.id, iteration=1, title="paper")
        second_literature = Literature(job_id=job.id, iteration=1, title="second paper")
        session.add_all(
            [
                hypothesis,
                second_hypothesis,
                finding,
                second_finding,
                literature,
                second_literature,
                Administrator(user_id=admin.id),
                APIKey(user_id=owner.id, name="owner-key", key_hash=uuid4().hex),
                JobShare(job_id=job.id, shared_with_user_id=editor.id, permission_level="edit"),
                JobShare(job_id=job.id, shared_with_user_id=viewer.id, permission_level="view"),
            ]
        )
        await session.flush()
        await session.execute(
            text("INSERT INTO finding_hypotheses (finding_id, hypothesis_id) VALUES (:f, :h)"),
            {"f": finding.id, "h": hypothesis.id},
        )
        await session.execute(
            text("INSERT INTO finding_literature (finding_id, literature_id) VALUES (:f, :l)"),
            {"f": finding.id, "l": literature.id},
        )
        await session.commit()
    ids = _Tenants(
        owner=owner.id,
        editor=editor.id,
        viewer=viewer.id,
        stranger=stranger.id,
        admin=admin.id,
        job=job.id,
        hypothesis=hypothesis.id,
        second_hypothesis=second_hypothesis.id,
        finding=finding.id,
        second_finding=second_finding.id,
        literature=literature.id,
        second_literature=second_literature.id,
    )
    yield ids
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": ids.job})
        await conn.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"),
            {"ids": [ids.owner, ids.editor, ids.viewer, ids.stranger, ids.admin]},
        )


async def _as_user(session: AsyncSession, user_id: UUID) -> None:
    await enable_rls(session)
    await set_current_user(session, user_id)


@pytest.mark.parametrize(("column", "assignment"), _PROTECTED_ASSIGNMENTS)
@pytest.mark.asyncio
async def test_an_edit_share_cannot_change_an_owner_field(
    db_session: AsyncSession, tenants: _Tenants, column: str, assignment: str
) -> None:
    """A policy cannot fence columns, so the grant on the app role does."""
    await _as_user(db_session, tenants.editor)
    with pytest.raises(DBAPIError, match="permission denied for table"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(f"UPDATE jobs SET {column} = {assignment} WHERE id = :job"),
                {"job": tenants.job, "stranger": tenants.stranger},
            )


@pytest.mark.asyncio
async def test_the_owner_cannot_reassign_the_job_through_the_web_role(
    db_session: AsyncSession, tenants: _Tenants
) -> None:
    """Orphan assignment runs on the admin login, never the app role."""
    await _as_user(db_session, tenants.owner)
    with pytest.raises(DBAPIError, match="permission denied for table"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE jobs SET owner_id = :stranger WHERE id = :job"),
                {"job": tenants.job, "stranger": tenants.stranger},
            )


@pytest.mark.asyncio
async def test_an_edit_share_changes_the_run_columns_the_web_updates(
    tenants: _Tenants, db_session: AsyncSession
) -> None:
    """The feedback-resume flip and the knowledge-state save stay open."""
    await _as_user(db_session, tenants.editor)
    await db_session.execute(
        text(
            "UPDATE jobs SET status = 'running', current_iteration = 2, "
            "agent_status = 'Resuming', agent_status_updated_at = now(), "
            "data_summary = '{\"iteration\": 2}'::jsonb, consensus_answer = 'P21 route' "
            "WHERE id = :job AND status = 'awaiting_feedback'"
        ),
        {"job": tenants.job},
    )
    row = (
        await db_session.execute(
            text(
                "SELECT status, current_iteration, agent_status, data_summary, "
                "consensus_answer FROM jobs WHERE id = :job"
            ),
            {"job": tenants.job},
        )
    ).one()
    assert row.status == "running"
    assert row.current_iteration == 2
    assert row.agent_status == "Resuming"
    assert row.data_summary == {"iteration": 2}
    assert row.consensus_answer == "P21 route"


@pytest.mark.asyncio
async def test_the_owner_may_write_the_run_columns_the_web_writes(
    tenants: _Tenants, db_session: AsyncSession
) -> None:
    """Status moves, queue notes and the provenance stamp stay open for the
    owner's own job."""
    await _as_user(db_session, tenants.owner)
    await db_session.execute(
        text(
            "UPDATE jobs SET status = 'queued', queue_reason = 'Waiting for a slot', "
            "error_message = 'last run failed', cancellation_reason = 'user stopped it', "
            "stop_note = 'budget reached', started_at = now(), ended_at = now(), "
            'version_info = \'{"openscientist_commit": "abc123"}\'::jsonb '
            "WHERE id = :job"
        ),
        {"job": tenants.job},
    )
    row = (
        await db_session.execute(
            text(
                "SELECT status, queue_reason, error_message, cancellation_reason, "
                "stop_note, started_at, ended_at, version_info FROM jobs WHERE id = :job"
            ),
            {"job": tenants.job},
        )
    ).one()
    assert row.status == "queued"
    assert row.queue_reason == "Waiting for a slot"
    assert row.error_message == "last run failed"
    assert row.cancellation_reason == "user stopped it"
    assert row.stop_note == "budget reached"
    assert row.started_at is not None
    assert row.ended_at is not None
    assert row.version_info == {"openscientist_commit": "abc123"}


@pytest.mark.asyncio
async def test_a_view_share_reads_finding_hypotheses_and_writes_nothing(
    db_session: AsyncSession, tenants: _Tenants
) -> None:
    """Any share reads the junction rows; only owners and edit shares write
    them."""
    await _as_user(db_session, tenants.viewer)
    row = (
        await db_session.execute(text("SELECT finding_id, hypothesis_id FROM finding_hypotheses"))
    ).one()
    assert (row.finding_id, row.hypothesis_id) == (tenants.finding, tenants.hypothesis)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("INSERT INTO finding_hypotheses (finding_id, hypothesis_id) VALUES (:f, :h)"),
                {"f": tenants.second_finding, "h": tenants.second_hypothesis},
            )
    # An update and a delete reach no row, so the seeded one is untouched.
    await db_session.execute(
        text("UPDATE finding_hypotheses SET hypothesis_id = :h WHERE finding_id = :f"),
        {"h": tenants.second_hypothesis, "f": tenants.finding},
    )
    await db_session.execute(text("DELETE FROM finding_hypotheses"))
    row = (
        await db_session.execute(text("SELECT finding_id, hypothesis_id FROM finding_hypotheses"))
    ).one()
    assert (row.finding_id, row.hypothesis_id) == (tenants.finding, tenants.hypothesis)


@pytest.mark.asyncio
async def test_an_edit_share_writes_finding_hypotheses_of_the_shared_job(
    tenants: _Tenants, db_session: AsyncSession
) -> None:
    await _as_user(db_session, tenants.editor)
    await db_session.execute(
        text("INSERT INTO finding_hypotheses (finding_id, hypothesis_id) VALUES (:f, :h)"),
        {"f": tenants.second_finding, "h": tenants.second_hypothesis},
    )
    # The update reaches the seeded row and the delete reaches both.
    await db_session.execute(
        text("UPDATE finding_hypotheses SET hypothesis_id = :h WHERE finding_id = :f"),
        {"h": tenants.second_hypothesis, "f": tenants.finding},
    )
    row = (
        await db_session.execute(
            text("SELECT hypothesis_id FROM finding_hypotheses WHERE finding_id = :f"),
            {"f": tenants.finding},
        )
    ).one()
    assert row.hypothesis_id == tenants.second_hypothesis
    await db_session.execute(text("DELETE FROM finding_hypotheses"))
    assert await db_session.scalar(text("SELECT count(*) FROM finding_hypotheses")) == 0


@pytest.mark.asyncio
async def test_a_view_share_reads_finding_literature_and_writes_nothing(
    db_session: AsyncSession, tenants: _Tenants
) -> None:
    await _as_user(db_session, tenants.viewer)
    row = (
        await db_session.execute(text("SELECT finding_id, literature_id FROM finding_literature"))
    ).one()
    assert (row.finding_id, row.literature_id) == (tenants.finding, tenants.literature)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("INSERT INTO finding_literature (finding_id, literature_id) VALUES (:f, :l)"),
                {"f": tenants.second_finding, "l": tenants.second_literature},
            )
    await db_session.execute(
        text("UPDATE finding_literature SET literature_id = :l WHERE finding_id = :f"),
        {"l": tenants.second_literature, "f": tenants.finding},
    )
    await db_session.execute(text("DELETE FROM finding_literature"))
    row = (
        await db_session.execute(text("SELECT finding_id, literature_id FROM finding_literature"))
    ).one()
    assert (row.finding_id, row.literature_id) == (tenants.finding, tenants.literature)


@pytest.mark.asyncio
async def test_an_edit_share_writes_finding_literature_of_the_shared_job(
    tenants: _Tenants, db_session: AsyncSession
) -> None:
    await _as_user(db_session, tenants.editor)
    await db_session.execute(
        text("INSERT INTO finding_literature (finding_id, literature_id) VALUES (:f, :l)"),
        {"f": tenants.second_finding, "l": tenants.second_literature},
    )
    await db_session.execute(
        text("UPDATE finding_literature SET literature_id = :l WHERE finding_id = :f"),
        {"l": tenants.second_literature, "f": tenants.finding},
    )
    row = (
        await db_session.execute(
            text("SELECT literature_id FROM finding_literature WHERE finding_id = :f"),
            {"f": tenants.finding},
        )
    ).one()
    assert row.literature_id == tenants.second_literature
    await db_session.execute(text("DELETE FROM finding_literature"))
    assert await db_session.scalar(text("SELECT count(*) FROM finding_literature")) == 0


@pytest_asyncio.fixture
async def fresh_login(
    test_engine: AsyncEngine, test_database_url: str
) -> AsyncGenerator[asyncpg.Connection, None]:
    """A login that belongs to no openscientist role."""
    role = f"grants_{uuid4().hex}"
    async with test_engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{_LOGIN_PASSWORD}'"))
    url = make_url(test_database_url).set(username=role, password=_LOGIN_PASSWORD)
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port,
        database=url.database,
        user=url.username,
        password=url.password,
    )
    try:
        yield conn
    finally:
        await conn.close()
        async with test_engine.begin() as db:
            await db.execute(text(f"DROP ROLE {role}"))


@pytest_asyncio.fixture
async def agent_login(
    test_engine: AsyncEngine, test_database_url: str, tenants: _Tenants
) -> AsyncGenerator[asyncpg.Connection, None]:
    """A container login for the shared job, as the launcher creates it."""
    role = f"agent_grants_{uuid4().hex}"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{_LOGIN_PASSWORD}' IN ROLE openscientist_agent"
            )
        )
        await conn.execute(
            text("INSERT INTO agent_job_roles (role_name, job_id) VALUES (:role, :job)"),
            {"role": role, "job": tenants.job},
        )
    url = make_url(test_database_url).set(username=role, password=_LOGIN_PASSWORD)
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port,
        database=url.database,
        user=url.username,
        password=url.password,
    )
    try:
        yield conn
    finally:
        await conn.close()
        async with test_engine.begin() as db:
            await db.execute(
                text("DELETE FROM agent_job_roles WHERE role_name = :role"), {"role": role}
            )
            await db.execute(text(f"DROP ROLE {role}"))


@pytest.mark.parametrize(("query", "nargs"), _HELPER_QUERIES)
@pytest.mark.asyncio
async def test_a_fresh_login_cannot_call_the_definer_helpers(
    fresh_login: asyncpg.Connection, tenants: _Tenants, query: str, nargs: int
) -> None:
    args = (tenants.job, str(tenants.stranger)) if nargs == 2 else (tenants.stranger,) * nargs
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await fresh_login.fetchval(query, *args)


@pytest.mark.asyncio
async def test_the_agent_login_keeps_its_helper_and_loses_the_others(
    agent_login: asyncpg.Connection, tenants: _Tenants
) -> None:
    assert await agent_login.fetchval("SELECT agent_job_id()") == tenants.job
    for query, nargs in _HELPER_QUERIES:
        args = (tenants.job, str(tenants.stranger)) if nargs == 2 else (tenants.stranger,) * nargs
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await agent_login.fetchval(query, *args)


@pytest.mark.asyncio
async def test_the_app_role_still_uses_the_helpers_through_its_policies(
    db_session: AsyncSession, tenants: _Tenants
) -> None:
    await enable_rls(db_session)
    await set_current_user(db_session, tenants.owner)
    assert (
        await db_session.scalar(
            text("SELECT check_job_owner(:job, :user)"),
            {"job": tenants.job, "user": str(tenants.owner)},
        )
        is True
    )
    assert (
        await db_session.scalar(
            text("SELECT check_job_owner(:job, :user)"),
            {"job": tenants.job, "user": str(tenants.stranger)},
        )
        is False
    )
    await set_current_user(db_session, tenants.admin)
    assert await db_session.scalar(text("SELECT current_user_is_admin()")) is True
    # The admin read goes through current_user_is_admin inside the policy.
    keys = await db_session.execute(text("SELECT id FROM api_keys"))
    assert keys.first() is not None


@pytest.mark.asyncio
async def test_the_app_role_cannot_truncate(db_session: AsyncSession, tenants: _Tenants) -> None:
    await _as_user(db_session, tenants.owner)
    with pytest.raises(DBAPIError, match="permission denied for table"):
        async with db_session.begin_nested():
            await db_session.execute(text("TRUNCATE users"))


@pytest.mark.asyncio
async def test_the_agent_login_cannot_truncate(agent_login: asyncpg.Connection) -> None:
    async with agent_login.transaction():
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await agent_login.execute("TRUNCATE findings")

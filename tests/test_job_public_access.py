"""Public job access: which tables a signed-in user can read once a job is public.

The owner flips ``is_public`` and any signed-in user with the link reads the job
and its analysis rows, but not its uploaded inputs, chat, costs or shares. Only
the owner may change the flag, and the agent login still reaches only its own
job.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy import Select, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import (
    AnalysisLog,
    CostRecord,
    FeedbackHistory,
    Finding,
    Hypothesis,
    IterationSummary,
    Job,
    JobChatMessage,
    JobDataFile,
    JobShare,
    Literature,
    Plot,
    User,
    finding_hypotheses,
    finding_literature,
)
from openscientist.database.rls import set_current_user
from tests.helpers import enable_rls

# The tables a public job opens up to any signed-in user.
ANALYSIS = frozenset(
    {
        "jobs",
        "hypotheses",
        "findings",
        "literature",
        "analysis_log",
        "iteration_summaries",
        "feedback_history",
        "plots",
        "finding_hypotheses",
        "finding_literature",
    }
)
# The tables a public job keeps owner/share-only.
WITHHELD = frozenset({"job_data_files", "job_chat_messages", "cost_records"})
SHARED = frozenset({"job_shares"})
ALL_TABLES = ANALYSIS | WITHHELD | SHARED


class _Seeded:
    def __init__(self, job: Job, ids: dict[str, UUID]) -> None:
        self.job = job
        self.ids = ids


async def _seed(db_session: AsyncSession, owner: User, *, is_public: bool) -> _Seeded:
    """A job with one row in every job table, plus view and edit shares."""
    job = Job(
        owner_id=owner.id,
        research_question="Public visibility job",
        description="visibility test",
        status="completed",
        is_public=is_public,
    )
    db_session.add(job)
    await db_session.flush()

    hypothesis = Hypothesis(job_id=job.id, iteration=1, text="Hypothesis")
    finding = Finding(
        job_id=job.id,
        iteration=1,
        text="Finding",
        finding_type="observation",
        source="analysis",
    )
    literature = Literature(job_id=job.id, iteration=1, title="A paper")
    analysis_log = AnalysisLog(
        job_id=job.id, iteration=1, step_number=1, action_type="analysis", description="Step"
    )
    iteration_summary = IterationSummary(job_id=job.id, iteration=1, summary_text="Summary")
    feedback_history = FeedbackHistory(
        job_id=job.id, iteration=1, feedback_type="note", feedback_text="Feedback"
    )
    plot = Plot(job_id=job.id, iteration=1, title="A plot", file_path="plots/plot.png")
    db_session.add_all(
        [
            hypothesis,
            finding,
            literature,
            analysis_log,
            iteration_summary,
            feedback_history,
            plot,
            JobDataFile(
                job_id=job.id,
                filename="cohort.csv",
                file_path="data/cohort.csv",
                file_type="csv",
                file_size=10,
            ),
            JobChatMessage(job_id=job.id, role="user", content="private note"),
            CostRecord(
                job_id=job.id,
                operation_type="agent",
                provider="vllm",
                model="m",
                input_tokens=1,
                output_tokens=1,
                cache_read_tokens=0,
                cache_write_tokens=0,
                reasoning_tokens=0,
                cost_usd=0.01,
            ),
        ]
    )
    await db_session.flush()
    await db_session.execute(
        finding_hypotheses.insert().values(finding_id=finding.id, hypothesis_id=hypothesis.id)
    )
    await db_session.execute(
        finding_literature.insert().values(finding_id=finding.id, literature_id=literature.id)
    )
    await db_session.commit()
    await db_session.refresh(job)
    return _Seeded(
        job,
        {
            "hypothesis": hypothesis.id,
            "finding": finding.id,
            "literature": literature.id,
        },
    )


def _table_selects(seeded: _Seeded) -> dict[str, Select]:
    job, ids = seeded.job, seeded.ids
    return {
        "jobs": select(Job.id).where(Job.id == job.id),
        "hypotheses": select(Hypothesis.id).where(Hypothesis.job_id == job.id),
        "findings": select(Finding.id).where(Finding.job_id == job.id),
        "literature": select(Literature.id).where(Literature.job_id == job.id),
        "analysis_log": select(AnalysisLog.id).where(AnalysisLog.job_id == job.id),
        "iteration_summaries": select(IterationSummary.id).where(IterationSummary.job_id == job.id),
        "feedback_history": select(FeedbackHistory.id).where(FeedbackHistory.job_id == job.id),
        "plots": select(Plot.id).where(Plot.job_id == job.id),
        "finding_hypotheses": select(finding_hypotheses.c.finding_id).where(
            finding_hypotheses.c.finding_id == ids["finding"]
        ),
        "finding_literature": select(finding_literature.c.finding_id).where(
            finding_literature.c.finding_id == ids["finding"]
        ),
        "job_data_files": select(JobDataFile.id).where(JobDataFile.job_id == job.id),
        "job_chat_messages": select(JobChatMessage.id).where(JobChatMessage.job_id == job.id),
        "cost_records": select(CostRecord.id).where(CostRecord.job_id == job.id),
        "job_shares": select(JobShare.id).where(JobShare.job_id == job.id),
    }


async def _visible_tables(
    db_session: AsyncSession, seeded: _Seeded, user_id: UUID | None
) -> frozenset[str]:
    """The tables of ``seeded`` a session acting as ``user_id`` can read."""
    await enable_rls(db_session)
    await set_current_user(db_session, user_id)
    visible: set[str] = set()
    for name, stmt in _table_selects(seeded).items():
        row = (await db_session.execute(stmt.limit(1))).first()
        if row is not None:
            visible.add(name)
    return frozenset(visible)


@pytest_asyncio.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(email=f"owner-{uuid4().hex}@pub.test", name="Owner")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def readers(db_session: AsyncSession) -> dict[str, User]:
    users = {}
    for label in ("view", "edit", "other"):
        user = User(email=f"{label}-{uuid4().hex}@pub.test", name=f"Reader {label}")
        db_session.add(user)
        users[label] = user
    await db_session.commit()
    for user in users.values():
        await db_session.refresh(user)
    return users


@pytest_asyncio.fixture
async def shares(db_session: AsyncSession, owner: User, readers: dict[str, User]) -> _Seeded:
    """A private job owned by ``owner``, shared with the view and edit readers."""
    seeded = await _seed(db_session, owner, is_public=False)
    db_session.add_all(
        [
            JobShare(
                job_id=seeded.job.id,
                shared_with_user_id=readers["view"].id,
                permission_level="view",
            ),
            JobShare(
                job_id=seeded.job.id,
                shared_with_user_id=readers["edit"].id,
                permission_level="edit",
            ),
        ]
    )
    await db_session.commit()
    return seeded


@pytest_asyncio.fixture
async def public_job(db_session: AsyncSession, owner: User) -> _Seeded:
    """A public job owned by ``owner``, shared with no one."""
    return await _seed(db_session, owner, is_public=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_public", [True, False])
async def test_owner_and_sharees_read_every_table(
    db_session: AsyncSession,
    owner: User,
    readers: dict[str, User],
    is_public: bool,
) -> None:
    seeded = await _seed(db_session, owner, is_public=is_public)
    db_session.add_all(
        [
            JobShare(
                job_id=seeded.job.id,
                shared_with_user_id=readers["view"].id,
                permission_level="view",
            ),
            JobShare(
                job_id=seeded.job.id,
                shared_with_user_id=readers["edit"].id,
                permission_level="edit",
            ),
        ]
    )
    await db_session.commit()

    for user in (owner, readers["view"], readers["edit"]):
        assert await _visible_tables(db_session, seeded, user.id) == ALL_TABLES, (
            f"{user.email} should read every table of their {is_public} job"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_public", [True, False])
async def test_public_opens_analysis_but_not_inputs_chat_costs_or_shares(
    db_session: AsyncSession,
    owner: User,
    readers: dict[str, User],
    is_public: bool,
) -> None:
    seeded = await _seed(db_session, owner, is_public=is_public)
    expected = ANALYSIS if is_public else frozenset()
    assert await _visible_tables(db_session, seeded, readers["other"].id) == expected, (
        f"a user with no share reads only the analysis tables of a public job "
        f"(is_public={is_public})"
    )


@pytest.mark.asyncio
async def test_a_session_with_no_user_reads_nothing(
    db_session: AsyncSession, public_job: _Seeded
) -> None:
    assert await _visible_tables(db_session, public_job, None) == frozenset()


async def _agent_login(test_engine: AsyncEngine, test_database_url: str, job_id: UUID):
    """A login role for one container, mapped to ``job_id`` like the launcher makes."""
    from sqlalchemy.engine import make_url

    role = f"agent_pub_{uuid4().hex}"
    password = "agent-public-login-password"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE openscientist_agent")
        )
        await conn.execute(
            text("INSERT INTO agent_job_roles (role_name, job_id) VALUES (:role, :job)"),
            {"role": role, "job": job_id},
        )
    url = make_url(test_database_url).set(username=role, password=password)
    yield url.render_as_string(hide_password=False)
    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM agent_job_roles WHERE role_name = :role"), {"role": role}
        )
        await conn.execute(text(f"DROP ROLE {role}"))


@pytest.mark.asyncio
@pytest.mark.parametrize("is_public", [True, False])
async def test_another_jobs_agent_login_reads_nothing(
    test_engine: AsyncEngine,
    test_database_url: str,
    is_public: bool,
) -> None:
    """A committed public job is still invisible to another job's agent login."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        agent_owner = User(email=f"agentowner-{uuid4().hex}@pub.test", name="Agent owner")
        public_owner = User(email=f"pubowner-{uuid4().hex}@pub.test", name="Public owner")
        session.add_all([agent_owner, public_owner])
        await session.flush()
        own_job = Job(owner_id=agent_owner.id, research_question="The agent's own job")
        public_job = Job(
            owner_id=public_owner.id, research_question="Public job", is_public=is_public
        )
        session.add_all([own_job, public_job])
        await session.flush()
        session.add_all(
            [
                Finding(
                    job_id=public_job.id,
                    iteration=1,
                    text="A finding",
                    finding_type="observation",
                    source="analysis",
                ),
                Hypothesis(job_id=public_job.id, iteration=1, text="A hypothesis"),
                JobDataFile(
                    job_id=public_job.id,
                    filename="cohort.csv",
                    file_path="data/cohort.csv",
                    file_type="csv",
                    file_size=10,
                ),
            ]
        )
        await session.commit()
        ids = {
            "own": own_job.id,
            "public": public_job.id,
            "agent_owner": agent_owner.id,
            "public_owner": public_owner.id,
        }

    agen = _agent_login(test_engine, test_database_url, ids["own"])
    login_url = await agen.__anext__()
    try:
        conn = await asyncpg.connect(login_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        try:
            public_id = str(ids["public"])
            for table, column in (
                ("jobs", "id"),
                ("findings", "job_id"),
                ("hypotheses", "job_id"),
                ("job_data_files", "job_id"),
            ):
                count = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE {column} = $1", public_id
                )
                assert count == 0, f"the agent login reads no other job's {table}"
        finally:
            await conn.close()
    finally:
        await agen.aclose()

    async with test_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM jobs WHERE id = ANY(:ids)"),
            {"ids": [ids["own"], ids["public"]]},
        )
        await conn.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"),
            {"ids": [ids["agent_owner"], ids["public_owner"]]},
        )


async def _is_public(db_session: AsyncSession, job_id: UUID) -> bool:
    return bool(
        (
            await db_session.execute(
                text("SELECT is_public FROM jobs WHERE id = :job_id"), {"job_id": job_id}
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_only_the_owner_can_change_is_public(
    db_session: AsyncSession, owner: User, readers: dict[str, User], shares: _Seeded
) -> None:
    job_id = shares.job.id
    await enable_rls(db_session)

    # The owner flips the flag freely.
    await set_current_user(db_session, owner.id)
    await db_session.execute(
        text("UPDATE jobs SET is_public = true WHERE id = :job_id"), {"job_id": job_id}
    )
    assert await _is_public(db_session, job_id) is True

    # An edit share may UPDATE the job, so the trigger is what stops it from
    # flipping the flag.
    await set_current_user(db_session, readers["edit"].id)
    with pytest.raises(DBAPIError, match="only the job owner can change is_public"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE jobs SET is_public = false WHERE id = :job_id"), {"job_id": job_id}
            )

    # A user with no share cannot even reach the row: RLS makes the update a no-op.
    await set_current_user(db_session, readers["other"].id)
    await db_session.execute(
        text("UPDATE jobs SET is_public = false WHERE id = :job_id"), {"job_id": job_id}
    )

    # The flag is still the owner's choice.
    assert await _is_public(db_session, job_id) is True

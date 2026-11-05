"""What a tool call's load and save of the knowledge state cost and store on a
job with a long history."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import AnalysisLog, Hypothesis, Job, Literature
from openscientist.knowledge_state import KnowledgeState

LOGGED = 200
ITEMS = 20


@pytest_asyncio.fixture
async def long_job(db_session: AsyncSession) -> str:
    """A running job with many stored log entries, hypotheses, findings and papers."""
    job = Job(research_question="Q", status="running", current_iteration=2, max_iterations=5)
    db_session.add(job)
    await db_session.commit()
    job_id = str(job.id)
    ks = await KnowledgeState.load_from_database(job_id, db_session)
    for n in range(ITEMS):
        ks.add_hypothesis(f"Hypothesis {n}")
        ks.add_finding(title=f"Finding {n}", evidence="e")
        ks.add_literature(pmid=str(n), title=f"Paper {n}", abstract="a")
    for n in range(LOGGED):
        ks.log_analysis(
            action="execute_code", code="print(1)", output="o" * 1000, description=f"Run {n}"
        )
    await ks.save_to_database(job_id, db_session)
    return job_id


@contextmanager
def recorded_statements(session: AsyncSession) -> Iterator[list[str]]:
    engine = session.bind.engine.sync_engine
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def _stored_runs(session: AsyncSession, job_id: str, description: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(AnalysisLog)
        .where(AnalysisLog.job_id == job_id, AnalysisLog.description == description)
    )
    return int(count or 0)


@pytest.mark.asyncio
async def test_a_tool_call_on_a_long_history_reads_no_log_output_and_few_rows(
    db_session: AsyncSession, long_job: str
) -> None:
    with recorded_statements(db_session) as statements:
        ks = await KnowledgeState.load_from_database(long_job, db_session)
        ks.log_analysis(action="execute_code", code="print(2)", output="new", description="New run")
        await ks.save_to_database(long_job, db_session)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert not [s for s in selects if "output_data" in s]
    assert len(statements) <= 20, statements
    assert await _stored_runs(db_session, long_job, "New run") == 1


@pytest.mark.asyncio
async def test_saving_a_state_twice_stores_its_new_log_entry_once(
    db_session: AsyncSession, long_job: str
) -> None:
    ks = await KnowledgeState.load_from_database(long_job, db_session)
    ks.log_analysis(action="search_pubmed", query="q", description="Search once")
    await ks.save_to_database(long_job, db_session)
    await ks.save_to_database(long_job, db_session)
    assert await _stored_runs(db_session, long_job, "Search once") == 1
    assert await _stored_runs(db_session, long_job, "Run 0") == 1


@pytest.mark.asyncio
async def test_a_save_updates_stored_rows_and_inserts_new_ones(
    db_session: AsyncSession, long_job: str
) -> None:
    ks = await KnowledgeState.load_from_database(long_job, db_session)
    ks.update_hypothesis("H003", {"status": "supported", "result": {"conclusion": "Held"}})
    ks.add_hypothesis("A new hypothesis")
    ks.add_literature(pmid="999", title="A new paper", abstract="b")
    await ks.save_to_database(long_job, db_session)

    result = await db_session.execute(
        select(Hypothesis.text, Hypothesis.status).where(Hypothesis.job_id == long_job)
    )
    statuses = {text: status for text, status in result.all()}
    assert len(statuses) == ITEMS + 1
    assert statuses["Hypothesis 2"] == "supported"
    assert statuses["Hypothesis 3"] == "pending"
    assert statuses["A new hypothesis"] == "pending"
    papers = await db_session.scalar(
        select(func.count()).select_from(Literature).where(Literature.job_id == long_job)
    )
    assert papers == ITEMS + 1

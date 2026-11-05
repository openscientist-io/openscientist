"""The progress a job list or a job page shows for a job, read from the database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database import session as db_session
from openscientist.database.models import AnalysisLog, Finding, Job, User
from openscientist.job.types import completed_iterations
from openscientist.job_manager import _load_progress
from tests.db_outage import Forwarder


@pytest_asyncio.fixture
async def running_job(test_engine: AsyncEngine) -> AsyncIterator[str]:
    """A running job in its third iteration with two findings and a logged code run."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@progress.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Q", status="running", current_iteration=3)
        db.add(job)
        await db.flush()
        db.add_all(
            [
                Finding(
                    job_id=job.id,
                    iteration=1,
                    text="First",
                    finding_type="observation",
                    source="code_execution",
                ),
                Finding(
                    job_id=job.id,
                    iteration=2,
                    text="Second",
                    finding_type="observation",
                    source="code_execution",
                ),
                AnalysisLog(
                    job_id=job.id,
                    iteration=2,
                    step_number=1,
                    action_type="execute_code",
                    description="Run",
                    output_data={"output": "x" * 10_000},
                ),
            ]
        )
        await db.commit()
    yield str(job.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.mark.asyncio
async def test_progress_counts_findings_and_iterations_without_reading_the_log(
    database: Forwarder, running_job: str
) -> None:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    engine = db_session.get_admin_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        progress = await _load_progress(running_job, "running", 0, 0)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert progress == (completed_iterations("running", 3), 2)
    assert not [s for s in statements if "analysis_log" in s]

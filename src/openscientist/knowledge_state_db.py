"""Database persistence for KnowledgeState."""

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models.analysis_log import AnalysisLog
from openscientist.database.models.feedback_history import FeedbackHistory
from openscientist.database.models.finding import Finding
from openscientist.database.models.hypothesis import Hypothesis
from openscientist.database.models.iteration_summary import IterationSummary
from openscientist.database.models.job import Job as JobModel
from openscientist.database.models.literature import Literature
from openscientist.database.session import SessionOpener, async_session_local
from openscientist.knowledge_state import KnowledgeState

T = TypeVar("T")

# Values here are JSONB payloads sourced from the schemaless knowledge-state
# dict, so the JSON boundary is genuinely dynamic.


def sanitize_for_json(obj: Any) -> Any:
    """Replace NaN/Infinity with None for JSONB compatibility."""
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(item) for item in obj]
    return obj


async def save_to_database_with_session(
    state: KnowledgeState, session: AsyncSession, job_id: str
) -> None:
    """Persist knowledge-state rows using an existing session."""
    job_uuid = UUID(job_id)
    await update_job_record(state, session, job_uuid)
    await upsert_hypotheses(state, session, job_uuid)
    await upsert_findings(state, session, job_uuid)
    await upsert_literature(state, session, job_uuid)
    await upsert_analysis_log(state, session, job_uuid)
    await upsert_iteration_summaries(state, session, job_uuid)
    await upsert_feedback_history(state, session, job_uuid)


async def first_scalar(session: AsyncSession, statement: Select[tuple[T]]) -> T | None:
    """Execute select statement and return first scalar result."""
    result = await session.execute(statement)
    return result.scalars().first()


async def rows_by_key(
    session: AsyncSession, statement: Select[tuple[T]], key: Callable[[T], Any]
) -> dict[Any, T]:
    """The rows ``statement`` selects, each under its ``key``, the first row
    winning when several share one."""
    rows: dict[Any, T] = {}
    for row in (await session.execute(statement)).scalars():
        rows.setdefault(key(row), row)
    return rows


async def update_job_record(state: KnowledgeState, session: AsyncSession, job_uuid: UUID) -> None:
    """Update job-level metadata persisted in the jobs table."""
    job = await first_scalar(session, select(JobModel).where(JobModel.id == job_uuid))
    if not job:
        return
    job.current_iteration = int(state.data.get("iteration", 1))
    job.data_summary = sanitize_for_json(state.data.get("data_summary") or {})
    job.agent_status = state.data.get("agent_status")
    updated_at_raw = state.data.get("agent_status_updated_at")
    if isinstance(updated_at_raw, str):
        try:
            job.agent_status_updated_at = datetime.fromisoformat(updated_at_raw)
        except ValueError:
            job.agent_status_updated_at = None
    else:
        job.agent_status_updated_at = None
    consensus_answer = state.data.get("consensus_answer")
    job.consensus_answer = consensus_answer if isinstance(consensus_answer, str) else None


async def upsert_hypotheses(state: KnowledgeState, session: AsyncSession, job_uuid: UUID) -> None:
    """Create or update hypotheses rows from knowledge state."""
    stored = await rows_by_key(
        session, select(Hypothesis).where(Hypothesis.job_id == job_uuid), lambda row: row.text
    )
    for hypothesis_data in state.data["hypotheses"]:
        existing = stored.get(hypothesis_data["statement"])
        if not existing:
            session.add(
                Hypothesis(
                    job_id=job_uuid,
                    iteration=hypothesis_data["iteration_proposed"],
                    text=hypothesis_data["statement"],
                    status=hypothesis_data["status"],
                    confidence=None,
                    priority=None,
                    rationale=f"Proposed by {hypothesis_data.get('proposed_by', 'agent')}",
                    test_strategy=hypothesis_data.get("test_code"),
                    supporting_evidence=sanitize_for_json(
                        {"result": hypothesis_data.get("result")}
                    ),
                )
            )
            continue
        existing.status = hypothesis_data["status"]
        existing.test_strategy = hypothesis_data.get("test_code")
        existing.supporting_evidence = sanitize_for_json({"result": hypothesis_data.get("result")})


def finding_payload(finding_data: dict[str, Any]) -> dict[str, Any]:
    """Build serialized finding payload for database storage."""
    return {
        "evidence": finding_data["evidence"],
        "supporting_hypotheses": finding_data.get("supporting_hypotheses", []),
        "literature_support": finding_data.get("literature_support", []),
        "plots": finding_data.get("plots", []),
        "biological_interpretation": finding_data.get("biological_interpretation", ""),
        "citations": finding_data.get("citations", []),
    }


async def upsert_findings(state: KnowledgeState, session: AsyncSession, job_uuid: UUID) -> None:
    """Create or update findings rows from knowledge state."""
    stored = await rows_by_key(
        session, select(Finding).where(Finding.job_id == job_uuid), lambda row: row.text
    )
    for finding_data in state.data["findings"]:
        existing = stored.get(finding_data["title"])
        if not existing:
            session.add(
                Finding(
                    job_id=job_uuid,
                    iteration=finding_data["iteration_discovered"],
                    text=finding_data["title"],
                    finding_type=finding_data.get("finding_type", "observation"),
                    source="code_execution",
                    data=sanitize_for_json(finding_payload(finding_data)),
                )
            )
            continue
        existing.finding_type = finding_data.get("finding_type", "observation")
        existing.data = sanitize_for_json(finding_payload(finding_data))


def literature_extra_metadata(literature_data: dict[str, Any]) -> dict[str, Any]:
    """Build normalized literature extra metadata payload."""
    return {
        "pmid": literature_data.get("pmid"),
        "search_query": literature_data.get("search_query"),
    }


async def upsert_literature(state: KnowledgeState, session: AsyncSession, job_uuid: UUID) -> None:
    """Create or update literature rows from knowledge state."""
    stored = await rows_by_key(
        session, select(Literature).where(Literature.job_id == job_uuid), lambda row: row.title
    )
    for literature_data in state.data["literature"]:
        existing = stored.get(literature_data["title"])
        if not existing:
            session.add(
                Literature(
                    job_id=job_uuid,
                    iteration=literature_data.get("retrieved_at_iteration", 1),
                    title=literature_data["title"],
                    abstract=literature_data.get("abstract"),
                    authors=literature_data.get("authors"),
                    year=literature_data.get("year"),
                    extra_metadata=literature_extra_metadata(literature_data),
                )
            )
            continue
        existing.abstract = literature_data.get("abstract")
        existing.authors = literature_data.get("authors")
        existing.year = literature_data.get("year")
        existing.extra_metadata = literature_extra_metadata(literature_data)


async def upsert_analysis_log(state: KnowledgeState, session: AsyncSession, job_uuid: UUID) -> None:
    """Insert the logged entries that are not already persisted.

    An entry is stored once per (iteration, description, timestamp), so saving
    the same state twice stores its entries once.
    """
    entries = [
        (
            log_entry,
            datetime.fromisoformat(log_entry["timestamp"]),
            str(log_entry.get("description") or log_entry.get("action", "")),
        )
        for log_entry in state.data["analysis_log"]
    ]
    if not entries:
        return
    stored = await session.execute(
        select(AnalysisLog.iteration, AnalysisLog.description, AnalysisLog.created_at).where(
            AnalysisLog.job_id == job_uuid,
            AnalysisLog.created_at.in_({timestamp for _, timestamp, _ in entries}),
        )
    )
    persisted = {tuple(row) for row in stored.all()}
    for log_entry, timestamp, description in entries:
        if (log_entry["iteration"], description, timestamp) in persisted:
            continue
        input_data = {
            key: value
            for key, value in log_entry.items()
            if key
            not in {
                "iteration",
                "action",
                "timestamp",
                "output",
                "success",
                "execution_time",
            }
        }
        output_data = {"output": log_entry.get("output")} if log_entry.get("output") else None
        session.add(
            AnalysisLog(
                job_id=job_uuid,
                iteration=log_entry["iteration"],
                step_number=1,
                action_type=log_entry["action"],
                description=description,
                input_data=sanitize_for_json(input_data) or None,
                output_data=sanitize_for_json(output_data),
                duration_seconds=(
                    float(log_entry["execution_time"])
                    if log_entry.get("execution_time") is not None
                    else None
                ),
                success=bool(log_entry.get("success", True)),
                created_at=timestamp,
            )
        )


async def upsert_iteration_summaries(
    state: KnowledgeState, session: AsyncSession, job_uuid: UUID
) -> None:
    """Create or update iteration summary rows."""
    stored = await rows_by_key(
        session,
        select(IterationSummary).where(IterationSummary.job_id == job_uuid),
        lambda row: row.iteration,
    )
    for summary_data in state.data.get("iteration_summaries", []):
        existing = stored.get(summary_data["iteration"])
        if not existing:
            session.add(
                IterationSummary(
                    job_id=job_uuid,
                    iteration=summary_data["iteration"],
                    summary_text=summary_data["summary"],
                    strapline=summary_data.get("strapline"),
                )
            )
            continue
        existing.summary_text = summary_data["summary"]
        existing.strapline = summary_data.get("strapline")


async def upsert_feedback_history(
    state: KnowledgeState, session: AsyncSession, job_uuid: UUID
) -> None:
    """Insert feedback history rows when missing."""
    stored = await rows_by_key(
        session,
        select(FeedbackHistory).where(FeedbackHistory.job_id == job_uuid),
        lambda row: row.iteration,
    )
    for feedback_data in state.data.get("feedback_history", []):
        existing = stored.get(feedback_data["after_iteration"])
        if existing:
            continue
        session.add(
            FeedbackHistory(
                job_id=job_uuid,
                iteration=feedback_data["after_iteration"],
                feedback_type="user_feedback",
                feedback_text=feedback_data["text"],
            )
        )


async def save_to_database(
    state: KnowledgeState, job_id: str, session: AsyncSession | None = None
) -> None:
    """Save knowledge state to database tables.

    The caller's session decides which rows the save may reach. Without one,
    the process's own login is used, which in the agent container is the
    container's job login.
    """
    if session is None:
        async with async_session_local() as own_session:
            await save_to_database_with_session(state, own_session, job_id)
            await own_session.commit()
        return

    await save_to_database_with_session(state, session, job_id)
    await session.commit()


async def load_from_database(job_id: str, session: AsyncSession | None = None) -> KnowledgeState:
    """Load knowledge state from database tables.

    The caller's session behaves as in :func:`save_to_database`.
    """
    if session is None:
        async with async_session_local() as own_session:
            return await load_with_session(own_session, job_id)
    return await load_with_session(session, job_id)


async def load_with_session(session: AsyncSession, job_id: str) -> KnowledgeState:
    """Load the knowledge state, with an analysis log that holds only what is
    logged after the load. The stored log rows carry every code run's full
    output, so they are read through the queries that need them."""
    job_uuid = UUID(job_id)
    job = await load_job_or_raise(session, job_id, job_uuid)
    ks = new_from_job_record(job_id, job)
    await load_hypotheses(session, job_uuid, ks)
    await load_findings(session, job_uuid, ks)
    await load_literature(session, job_uuid, ks)
    await load_iteration_summaries(session, job_uuid, ks)
    await load_feedback_history(session, job_uuid, ks)
    return ks


async def load_job_or_raise(session: AsyncSession, job_id: str, job_uuid: UUID) -> JobModel:
    """Load job row and raise when it does not exist."""
    result = await session.execute(select(JobModel).where(JobModel.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None:
        raise ValueError(f"Job {job_id} not found in database")
    return job


def new_from_job_record(job_id: str, job: JobModel) -> KnowledgeState:
    """Create a KnowledgeState instance seeded from jobs table metadata."""
    ks = KnowledgeState.__new__(KnowledgeState)
    ks.data = {
        "config": {
            "job_id": job_id,
            "research_question": job.research_question,
            "max_iterations": job.max_iterations,
            "started_at": job.created_at.isoformat(),
        },
        "data_summary": job.data_summary or {},
        "iteration": job.current_iteration,
        "hypotheses": [],
        "findings": [],
        "literature": [],
        "analysis_log": [],
        "iteration_summaries": [],
        "feedback_history": [],
        "agent_status": job.agent_status,
        "agent_status_updated_at": (
            job.agent_status_updated_at.isoformat() if job.agent_status_updated_at else None
        ),
        "consensus_answer": job.consensus_answer,
    }
    return ks


async def load_hypotheses(session: AsyncSession, job_uuid: UUID, ks: KnowledgeState) -> None:
    """Load hypotheses rows into knowledge state format."""
    result = await session.execute(
        select(Hypothesis).where(Hypothesis.job_id == job_uuid).order_by(Hypothesis.created_at)
    )
    for hypothesis in result.scalars().all():
        ks.data["hypotheses"].append(
            {
                "id": f"H{len(ks.data['hypotheses']) + 1:03d}",
                "iteration_proposed": hypothesis.iteration,
                "statement": hypothesis.text,
                "status": hypothesis.status,
                "proposed_by": "agent",
                "tested_at_iteration": None,
                "test_code": hypothesis.test_strategy,
                "result": (
                    hypothesis.supporting_evidence.get("result")
                    if hypothesis.supporting_evidence
                    else None
                ),
                "spawned_hypotheses": [],
            }
        )


async def load_findings(session: AsyncSession, job_uuid: UUID, ks: KnowledgeState) -> None:
    """Load findings rows into knowledge state format."""
    result = await session.execute(
        select(Finding).where(Finding.job_id == job_uuid).order_by(Finding.created_at)
    )
    for finding in result.scalars().all():
        finding_data = finding.data or {}
        ks.data["findings"].append(
            {
                "id": f"F{len(ks.data['findings']) + 1:03d}",
                "iteration_discovered": finding.iteration,
                "title": finding.text,
                "evidence": finding_data.get("evidence", ""),
                "supporting_hypotheses": finding_data.get("supporting_hypotheses", []),
                "literature_support": finding_data.get("literature_support", []),
                "plots": finding_data.get("plots", []),
                "biological_interpretation": finding_data.get("biological_interpretation", ""),
                "citations": finding_data.get("citations", []),
            }
        )


async def load_literature(session: AsyncSession, job_uuid: UUID, ks: KnowledgeState) -> None:
    """Load literature rows into knowledge state format."""
    result = await session.execute(
        select(Literature).where(Literature.job_id == job_uuid).order_by(Literature.created_at)
    )
    for literature in result.scalars().all():
        extra = literature.extra_metadata or {}
        ks.data["literature"].append(
            {
                "id": f"L{len(ks.data['literature']) + 1:03d}",
                "pmid": extra.get("pmid", ""),
                "title": literature.title,
                "abstract": literature.abstract,
                "relevance_to": [],
                "retrieved_at_iteration": literature.iteration,
                "search_query": extra.get("search_query"),
            }
        )


def _serialized_log_entry(
    iteration: int,
    action_type: str,
    created_at: datetime,
    description: str,
    success: bool | None,
    duration_seconds: float | None,
    input_data: dict[str, Any] | None,
    output_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """One analysis log row in knowledge state format."""
    serialized: dict[str, Any] = {
        "iteration": iteration,
        "action": action_type,
        "timestamp": created_at.isoformat(),
        "description": description,
        "success": bool(success),
    }
    if duration_seconds is not None:
        serialized["execution_time"] = duration_seconds
    if input_data:
        serialized.update(input_data)
    if output_data:
        serialized.update(output_data)
    return serialized


async def analysis_log_action_counts(
    session: AsyncSession, job_uuid: UUID
) -> dict[int, dict[str, int]]:
    """How many log rows of each action type every iteration of the job holds."""
    result = await session.execute(
        select(AnalysisLog.iteration, AnalysisLog.action_type, func.count())
        .where(AnalysisLog.job_id == job_uuid)
        .group_by(AnalysisLog.iteration, AnalysisLog.action_type)
    )
    counts: dict[int, dict[str, int]] = {}
    for iteration, action_type, count in result.all():
        counts.setdefault(iteration, {})[action_type] = count
    return counts


async def load_iteration_analysis_log(
    session: AsyncSession,
    job_uuid: UUID,
    iteration: int,
    *,
    output_chars: int,
    action_type: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """One iteration's log rows in knowledge state format, oldest first, each
    output cut to ``output_chars`` plus one character, so a caller can tell a
    cut output from a whole one."""
    output = func.left(AnalysisLog.output_data["output"].astext, output_chars + 1).label("output")
    query = select(
        AnalysisLog.action_type,
        AnalysisLog.created_at,
        AnalysisLog.description,
        AnalysisLog.success,
        AnalysisLog.duration_seconds,
        AnalysisLog.input_data,
        output,
    ).where(AnalysisLog.job_id == job_uuid, AnalysisLog.iteration == iteration)
    if action_type is not None:
        query = query.where(AnalysisLog.action_type == action_type)
    query = query.order_by(AnalysisLog.created_at, AnalysisLog.id).offset(offset).limit(limit)
    result = await session.execute(query)
    return [
        _serialized_log_entry(
            iteration,
            row.action_type,
            row.created_at,
            row.description,
            row.success,
            row.duration_seconds,
            row.input_data,
            {"output": row.output} if row.output is not None else None,
        )
        for row in result.all()
    ]


async def load_iteration_summaries(
    session: AsyncSession, job_uuid: UUID, ks: KnowledgeState
) -> None:
    """Load iteration summary rows into knowledge state format."""
    result = await session.execute(
        select(IterationSummary)
        .where(IterationSummary.job_id == job_uuid)
        .order_by(IterationSummary.iteration)
    )
    for summary in result.scalars().all():
        ks.data["iteration_summaries"].append(
            {
                "iteration": summary.iteration,
                "summary": summary.summary_text,
                "strapline": summary.strapline or "",
                "created_at": summary.created_at.isoformat(),
            }
        )


async def load_feedback_history(session: AsyncSession, job_uuid: UUID, ks: KnowledgeState) -> None:
    """Load feedback history rows into knowledge state format."""
    result = await session.execute(
        select(FeedbackHistory)
        .where(FeedbackHistory.job_id == job_uuid)
        .order_by(FeedbackHistory.created_at)
    )
    for feedback in result.scalars().all():
        ks.data["feedback_history"].append(
            {
                "after_iteration": feedback.iteration,
                "text": feedback.feedback_text,
                "submitted_at": feedback.created_at.isoformat(),
            }
        )


def save_to_database_sync(
    state: KnowledgeState, job_id: str, open_session: SessionOpener | None = None
) -> None:
    """Run save_to_database in ``open_session``, opened inside the wrapper's own loop."""
    from openscientist.async_tasks import run_sync

    async def save() -> None:
        if open_session is None:
            await state.save_to_database(job_id)
            return
        async with open_session() as session:
            await state.save_to_database(job_id, session)

    run_sync(save())

"""
Knowledge state management for OpenScientist.
"""

import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models.analysis_log import AnalysisLog
from openscientist.database.models.feedback_history import FeedbackHistory
from openscientist.database.models.finding import Finding
from openscientist.database.models.hypothesis import Hypothesis
from openscientist.database.models.iteration_summary import IterationSummary
from openscientist.database.models.job import Job as JobModel
from openscientist.database.models.literature import Literature
from openscientist.database.session import SessionOpener, async_session_local

# Bound for the literature abstracts in the report prompt. Dumping every
# abstract overflows the model's context window and drops the tool definitions
# before the file-write call is emitted. Applies only when no budget is given.
_DEFAULT_REPORT_ABSTRACT_BUDGET_CHARS = 8000


def _sanitize_for_json(obj: Any) -> Any:
    """Replace NaN/Infinity with None for JSONB compatibility."""
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(item) for item in obj]
    return obj


class KnowledgeState:
    """Database-backed knowledge state for storing agent state."""

    def __init__(
        self,
        job_id: str,
        research_question: str,
        max_iterations: int,
        use_skills: bool = True,
    ):
        """Initialize a new knowledge graph."""
        self.data: dict[str, Any] = {
            "config": {
                "job_id": job_id,
                "research_question": research_question,
                "max_iterations": max_iterations,
                "use_skills": use_skills,
                "started_at": datetime.now(UTC).isoformat(),
            },
            "data_summary": {},
            "iteration": 1,
            "hypotheses": [],
            "findings": [],
            "literature": [],
            "analysis_log": [],
            "iteration_summaries": [],  # Agent-generated summaries per iteration
            "feedback_history": [],  # Scientist feedback: [{after_iteration, text, submitted_at}]
            "agent_status": None,  # Current agent status message (set by agent)
            "agent_status_updated_at": None,  # When status was last updated
            "consensus_answer": None,  # Consensus answer to research question (if reached)
        }

    def set_data_summary(self, summary: dict[str, Any]) -> None:
        """Set data summary (files, samples, features, etc.)."""
        self.data["data_summary"] = summary

    def add_hypothesis(self, statement: str, proposed_by: str = "agent") -> str:
        """Add a new hypothesis."""
        hypothesis_id = f"H{len(self.data['hypotheses']) + 1:03d}"

        hypothesis = {
            "id": hypothesis_id,
            "iteration_proposed": self.data["iteration"],
            "statement": statement,
            "status": "pending",  # pending, testing, supported, rejected
            "proposed_by": proposed_by,
            "tested_at_iteration": None,
            "test_code": None,
            "result": None,
            "spawned_hypotheses": [],
        }

        self.data["hypotheses"].append(hypothesis)
        return hypothesis_id

    def update_hypothesis(self, hypothesis_id: str, updates: dict[str, Any]) -> None:
        """Update a hypothesis with test results."""
        for hyp in self.data["hypotheses"]:
            if hyp["id"] == hypothesis_id:
                hyp.update(updates)
                return
        raise ValueError(f"Hypothesis {hypothesis_id} not found")

    def add_finding(
        self,
        title: str,
        evidence: str,
        supporting_hypotheses: list[str] | None = None,
        literature_support: list[str] | None = None,
        plots: list[str] | None = None,
        citations: list[dict[str, str]] | None = None,
    ) -> str:
        """Add a confirmed finding.

        Citations are validated against stored abstracts.
        """
        finding_id = f"F{len(self.data['findings']) + 1:03d}"

        validated_citations = [self.validate_citation(c) for c in (citations or [])]

        finding = {
            "id": finding_id,
            "iteration_discovered": self.data["iteration"],
            "title": title,
            "evidence": evidence,
            "supporting_hypotheses": supporting_hypotheses or [],
            "literature_support": literature_support or [],
            "plots": plots or [],
            "biological_interpretation": "",
            "citations": validated_citations,
        }

        self.data["findings"].append(finding)
        return finding_id

    def validate_citation(self, citation: dict[str, str]) -> dict[str, str]:
        """Validate a citation snippet against the stored abstract.

        Sets ``validation_status`` to ``verified``, ``verified_normalized``,
        ``mismatch`` or ``unchecked`` (PMID not in the literature list).
        """
        import re

        result = dict(citation)
        pmid = result.get("pmid", "")
        snippet = result.get("snippet", "")

        abstract = None
        for lit in self.data["literature"]:
            if str(lit.get("pmid")) == str(pmid):
                abstract = lit.get("abstract", "")
                break

        if abstract is None:
            result["validation_status"] = "unchecked"
        elif not snippet:
            result["validation_status"] = "mismatch"
        elif snippet in abstract:
            result["validation_status"] = "verified"
        else:

            def norm(s: str) -> str:  # noqa: E731
                s = re.sub(r"\s+", " ", s.lower().strip())
                return s.strip(".,;:!?\"'()[]{}")

            if norm(snippet) in norm(abstract):
                result["validation_status"] = "verified_normalized"
            else:
                result["validation_status"] = "mismatch"

        return result

    def add_literature(
        self,
        pmid: str,
        title: str,
        abstract: str,
        relevance_to: list[str] | None = None,
        search_query: str | None = None,
    ) -> str:
        """Add a literature reference."""
        literature_id = f"L{len(self.data['literature']) + 1:03d}"

        literature = {
            "id": literature_id,
            "pmid": pmid,
            "title": title,
            "abstract": abstract,
            "relevance_to": relevance_to or [],
            "retrieved_at_iteration": self.data["iteration"],
            "search_query": search_query,
        }

        self.data["literature"].append(literature)
        return literature_id

    def log_analysis(
        self,
        action: str,
        code: str | None = None,
        output: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Log an analysis action."""
        log_entry = {
            "iteration": self.data["iteration"],
            "action": action,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }

        if code:
            log_entry["code"] = code
        if output:
            log_entry["output"] = output

        self.data["analysis_log"].append(log_entry)

    def add_iteration_summary(self, iteration: int, summary: str, strapline: str = "") -> None:
        """Store agent-generated summary for an iteration.

        The strapline is a short title of 5-10 words.
        """
        for existing in self.data["iteration_summaries"]:
            if existing["iteration"] == iteration:
                existing["summary"] = summary
                existing["strapline"] = strapline
                existing["updated_at"] = datetime.now(UTC).isoformat()
                return

        self.data["iteration_summaries"].append(
            {
                "iteration": iteration,
                "summary": summary,
                "strapline": strapline,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    def set_agent_status(self, status: str) -> None:
        """Set the current agent status message, shown in the UI.

        Keep it under 80 characters.
        """
        self.data["agent_status"] = status
        self.data["agent_status_updated_at"] = datetime.now(UTC).isoformat()

    def add_feedback(self, text: str, after_iteration: int) -> None:
        """Add scientist feedback to history.

        Feedback is given after an iteration completes and is injected
        into the next iteration (N+1).
        """
        self.data["feedback_history"].append(
            {
                "after_iteration": after_iteration,
                "text": text,
                "submitted_at": datetime.now(UTC).isoformat(),
            }
        )

    def get_feedback_for_iteration(self, iteration: int) -> str | None:
        """Get feedback that should be injected into this iteration.

        Looks for feedback given after the previous iteration (N-1).
        """
        feedback_list = self.data.get("feedback_history", [])
        previous_iteration = iteration - 1

        matching = [f for f in feedback_list if f.get("after_iteration") == previous_iteration]
        if matching:
            return str(matching[-1]["text"])  # Use most recent if multiple
        return None

    def get_summary(self) -> str:
        """Get a text summary of current state for prompts."""
        summary_parts = [
            f"# Knowledge Graph Summary (Iteration {self.data['iteration']})",
            "",
            "## Research Question",
            self.data["config"]["research_question"],
            "",
            "## Data",
            f"- Files: {self.data['data_summary'].get('files', [])}",
            "",
            "## Progress",
            f"- Hypotheses tested: {len([h for h in self.data['hypotheses'] if h['status'] != 'pending'])}",
            f"- Findings confirmed: {len(self.data['findings'])}",
            f"- Literature reviewed: {len(self.data['literature'])}",
            "",
        ]

        # Recent findings
        if self.data["findings"]:
            summary_parts.append("## Recent Findings")
            summary_parts.extend(
                f"- **{finding['title']}**: {finding['evidence']}"
                for finding in self.data["findings"][-3:]
            )
            summary_parts.append("")

        pending = [h for h in self.data["hypotheses"] if h["status"] == "pending"]
        if pending:
            summary_parts.append("## Pending Hypotheses")
            summary_parts.extend(f"- {hyp['id']}: {hyp['statement']}" for hyp in pending[-3:])
            summary_parts.append("")

        rejected = [h for h in self.data["hypotheses"] if h["status"] == "rejected"]
        if rejected:
            summary_parts.append("## Rejected Hypotheses (avoid repeating)")
            summary_parts.extend(
                f"- {hyp['id']}: {hyp['statement']} - {hyp.get('result', {}).get('conclusion', 'No conclusion')}"
                for hyp in rejected[-3:]
            )
            summary_parts.append("")

        return "\n".join(summary_parts)

    def get_report_outline(self, *, abstract_budget_chars: int | None = None) -> str:
        """Get an outline of accumulated knowledge for the report prompt.

        ``abstract_budget_chars`` bounds the uncited-paper abstracts included
        for citation grounding. The caller derives it from the model's context
        window so the prompt cannot overflow it.
        """
        budget = (
            abstract_budget_chars
            if abstract_budget_chars is not None
            else _DEFAULT_REPORT_ABSTRACT_BUDGET_CHARS
        )
        parts: list[str] = [
            f"# Knowledge Outline ({self.data['iteration']} iterations completed)",
            "",
            "## Research Question",
            self.data["config"]["research_question"],
            "",
        ]

        all_hyps = self.data["hypotheses"]
        supported = [h for h in all_hyps if h["status"] == "supported"]
        rejected = [h for h in all_hyps if h["status"] == "rejected"]
        parts.append("## Progress")
        parts.append(f"- {len(self.data['findings'])} findings confirmed")
        parts.append(
            f"- {len(all_hyps)} hypotheses ({len(supported)} supported, {len(rejected)} rejected)"
        )
        parts.append(f"- {len(self.data['literature'])} papers reviewed")
        parts.append("")

        # Investigation timeline — straplines only
        summaries = self.data.get("iteration_summaries", [])
        if summaries:
            parts.append("## Investigation Timeline")
            for entry in sorted(summaries, key=lambda e: e["iteration"]):
                strapline = entry.get("strapline", entry.get("summary", "")[:120])
                parts.append(f"- Iteration {entry['iteration']}: {strapline}")
            parts.append("")

        if self.data["findings"]:
            parts.append("## Findings")
            for finding in self.data["findings"]:
                parts.append(f"- {finding['id']}: {finding['title']}")
                if finding.get("evidence"):
                    parts.append(f"  Statistical evidence: {finding['evidence']}")
                for c in finding.get("citations", []):
                    status = c.get("validation_status", "unchecked")
                    pmid = c.get("pmid", "?")
                    snippet = c.get("snippet", "")
                    explanation = c.get("explanation", "")
                    parts.append(f'  - PMID:{pmid} [{status}]: "{snippet}"')
                    if explanation:
                        parts.append(f"    → {explanation}")
            parts.append("")

        if all_hyps:
            parts.append("## Hypotheses")
            parts.extend(f"- {hyp['id']} [{hyp['status']}]: {hyp['statement']}" for hyp in all_hyps)
            parts.append("")

        # Cited papers get title+PMID only, since the citation snippet is the
        # grounding. Uncited papers include full abstracts as fallback.
        cited_pmids: set[str] = set()
        for finding in self.data["findings"]:
            for c in finding.get("citations", []):
                if c.get("pmid"):
                    cited_pmids.add(str(c["pmid"]))

        if self.data["literature"]:
            parts.append(f"## Literature ({len(self.data['literature'])} papers)")
            abstract_chars_spent = 0
            abstracts_omitted = 0
            for lit in self.data["literature"]:
                pmid_str = f" (PMID: {lit['pmid']})" if lit.get("pmid") else ""
                parts.append(f"- **{lit['title']}**{pmid_str}")
                # Full abstracts until the budget is reached, so a large list cannot overflow the context.
                if str(lit.get("pmid", "")) not in cited_pmids:
                    abstract = lit.get("abstract", "")
                    if not abstract:
                        continue
                    if abstract_chars_spent + len(abstract) > budget:
                        abstracts_omitted += 1
                        continue
                    parts.append(f"  Abstract: {abstract}")
                    abstract_chars_spent += len(abstract)
            if abstracts_omitted:
                parts.append(
                    f"\n_({abstracts_omitted} further abstracts omitted to fit the model "
                    "context. Cite papers through the findings above, not from titles alone.)_"
                )
            parts.append("")

        if self.data.get("consensus_answer"):
            parts.append("## Current Consensus Answer")
            parts.append(self.data["consensus_answer"])
            parts.append("")

        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Return the raw data dictionary."""
        return self.data

    # Database persistence methods

    async def _save_to_database_with_session(self, session: AsyncSession, job_id: str) -> None:
        """Persist knowledge-state rows using an existing session."""
        job_uuid = UUID(job_id)
        await self._update_job_record(session, job_uuid)
        await self._upsert_hypotheses(session, job_uuid)
        await self._upsert_findings(session, job_uuid)
        await self._upsert_literature(session, job_uuid)
        await self._upsert_analysis_log(session, job_uuid)
        await self._upsert_iteration_summaries(session, job_uuid)
        await self._upsert_feedback_history(session, job_uuid)

    @staticmethod
    async def _first_scalar(session: AsyncSession, statement: Any) -> Any:
        """Execute select statement and return first scalar result."""
        result = await session.execute(statement)
        return result.scalars().first()

    async def _update_job_record(self, session: AsyncSession, job_uuid: UUID) -> None:
        """Update job-level metadata persisted in the jobs table."""
        job = await self._first_scalar(session, select(JobModel).where(JobModel.id == job_uuid))
        if not job:
            return
        job.current_iteration = int(self.data.get("iteration", 1))
        job.data_summary = _sanitize_for_json(self.data.get("data_summary") or {})
        job.agent_status = self.data.get("agent_status")
        updated_at_raw = self.data.get("agent_status_updated_at")
        if isinstance(updated_at_raw, str):
            try:
                job.agent_status_updated_at = datetime.fromisoformat(updated_at_raw)
            except ValueError:
                job.agent_status_updated_at = None
        else:
            job.agent_status_updated_at = None
        consensus_answer = self.data.get("consensus_answer")
        job.consensus_answer = consensus_answer if isinstance(consensus_answer, str) else None

    async def _upsert_hypotheses(self, session: AsyncSession, job_uuid: UUID) -> None:
        """Create or update hypotheses rows from knowledge state."""
        for hypothesis_data in self.data["hypotheses"]:
            existing = await self._first_scalar(
                session,
                select(Hypothesis).where(
                    Hypothesis.job_id == job_uuid,
                    Hypothesis.text == hypothesis_data["statement"],
                ),
            )
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
                        supporting_evidence=_sanitize_for_json(
                            {"result": hypothesis_data.get("result")}
                        ),
                    )
                )
                continue
            existing.status = hypothesis_data["status"]
            existing.test_strategy = hypothesis_data.get("test_code")
            existing.supporting_evidence = _sanitize_for_json(
                {"result": hypothesis_data.get("result")}
            )

    @staticmethod
    def _finding_payload(finding_data: dict[str, Any]) -> dict[str, Any]:
        """Build serialized finding payload for database storage."""
        return {
            "evidence": finding_data["evidence"],
            "supporting_hypotheses": finding_data.get("supporting_hypotheses", []),
            "literature_support": finding_data.get("literature_support", []),
            "plots": finding_data.get("plots", []),
            "biological_interpretation": finding_data.get("biological_interpretation", ""),
            "citations": finding_data.get("citations", []),
        }

    async def _upsert_findings(self, session: AsyncSession, job_uuid: UUID) -> None:
        """Create or update findings rows from knowledge state."""
        for finding_data in self.data["findings"]:
            existing = await self._first_scalar(
                session,
                select(Finding).where(
                    Finding.job_id == job_uuid,
                    Finding.text == finding_data["title"],
                ),
            )
            if not existing:
                session.add(
                    Finding(
                        job_id=job_uuid,
                        iteration=finding_data["iteration_discovered"],
                        text=finding_data["title"],
                        finding_type=finding_data.get("finding_type", "observation"),
                        source="code_execution",
                        data=_sanitize_for_json(self._finding_payload(finding_data)),
                    )
                )
                continue
            existing.finding_type = finding_data.get("finding_type", "observation")
            existing.data = _sanitize_for_json(self._finding_payload(finding_data))

    @staticmethod
    def _literature_extra_metadata(literature_data: dict[str, Any]) -> dict[str, Any]:
        """Build normalized literature extra metadata payload."""
        return {
            "pmid": literature_data.get("pmid"),
            "search_query": literature_data.get("search_query"),
        }

    async def _upsert_literature(self, session: AsyncSession, job_uuid: UUID) -> None:
        """Create or update literature rows from knowledge state."""
        for literature_data in self.data["literature"]:
            existing = await self._first_scalar(
                session,
                select(Literature).where(
                    Literature.job_id == job_uuid,
                    Literature.title == literature_data["title"],
                ),
            )
            if not existing:
                session.add(
                    Literature(
                        job_id=job_uuid,
                        iteration=literature_data.get("retrieved_at_iteration", 1),
                        title=literature_data["title"],
                        abstract=literature_data.get("abstract"),
                        authors=literature_data.get("authors"),
                        journal=literature_data.get("journal"),
                        year=literature_data.get("year"),
                        doi=literature_data.get("doi"),
                        extra_metadata=self._literature_extra_metadata(literature_data),
                    )
                )
                continue
            existing.abstract = literature_data.get("abstract")
            existing.authors = literature_data.get("authors")
            existing.journal = literature_data.get("journal")
            existing.year = literature_data.get("year")
            existing.doi = literature_data.get("doi")
            existing.extra_metadata = self._literature_extra_metadata(literature_data)

    async def _upsert_analysis_log(self, session: AsyncSession, job_uuid: UUID) -> None:
        """Insert analysis log rows that are not already persisted."""
        for log_entry in self.data["analysis_log"]:
            timestamp = datetime.fromisoformat(log_entry["timestamp"])
            description = str(log_entry.get("description") or log_entry.get("action", ""))
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
            existing = await self._first_scalar(
                session,
                select(AnalysisLog).where(
                    AnalysisLog.job_id == job_uuid,
                    AnalysisLog.iteration == log_entry["iteration"],
                    AnalysisLog.description == description,
                    AnalysisLog.created_at == timestamp,
                ),
            )
            if existing:
                continue
            session.add(
                AnalysisLog(
                    job_id=job_uuid,
                    iteration=log_entry["iteration"],
                    step_number=1,
                    action_type=log_entry["action"],
                    description=description,
                    input_data=_sanitize_for_json(input_data) or None,
                    output_data=_sanitize_for_json(output_data),
                    duration_seconds=(
                        float(log_entry["execution_time"])
                        if log_entry.get("execution_time") is not None
                        else None
                    ),
                    success=bool(log_entry.get("success", True)),
                    created_at=timestamp,
                )
            )

    async def _upsert_iteration_summaries(self, session: AsyncSession, job_uuid: UUID) -> None:
        """Create or update iteration summary rows."""
        for summary_data in self.data.get("iteration_summaries", []):
            existing = await self._first_scalar(
                session,
                select(IterationSummary).where(
                    IterationSummary.job_id == job_uuid,
                    IterationSummary.iteration == summary_data["iteration"],
                ),
            )
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

    async def _upsert_feedback_history(self, session: AsyncSession, job_uuid: UUID) -> None:
        """Insert feedback history rows when missing."""
        for feedback_data in self.data.get("feedback_history", []):
            existing = await self._first_scalar(
                session,
                select(FeedbackHistory).where(
                    FeedbackHistory.job_id == job_uuid,
                    FeedbackHistory.iteration == feedback_data["after_iteration"],
                ),
            )
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

    async def save_to_database(self, job_id: str, session: AsyncSession | None = None) -> None:
        """Save knowledge state to database tables.

        The caller's session decides which rows the save may reach. Without one,
        the process's own login is used, which in the agent container is the
        container's job login.
        """
        if session is None:
            async with async_session_local() as own_session:
                await self._save_to_database_with_session(own_session, job_id)
                await own_session.commit()
            return

        await self._save_to_database_with_session(session, job_id)
        await session.commit()

    @classmethod
    async def load_from_database(
        cls, job_id: str, session: AsyncSession | None = None
    ) -> "KnowledgeState":
        """Load knowledge state from database tables.

        The caller's session behaves as in :meth:`save_to_database`.
        """
        if session is None:
            async with async_session_local() as own_session:
                return await cls._load_with_session(own_session, job_id)
        return await cls._load_with_session(session, job_id)

    @classmethod
    async def _load_with_session(cls, session: AsyncSession, job_id: str) -> "KnowledgeState":
        job_uuid = UUID(job_id)
        job = await cls._load_job_or_raise(session, job_id, job_uuid)
        ks = cls._new_from_job_record(job_id, job)
        await cls._load_hypotheses(session, job_uuid, ks)
        await cls._load_findings(session, job_uuid, ks)
        await cls._load_literature(session, job_uuid, ks)
        await cls._load_analysis_log(session, job_uuid, ks)
        await cls._load_iteration_summaries(session, job_uuid, ks)
        await cls._load_feedback_history(session, job_uuid, ks)
        return ks

    @classmethod
    async def _load_job_or_raise(
        cls, session: AsyncSession, job_id: str, job_uuid: UUID
    ) -> JobModel:
        """Load job row and raise when it does not exist."""
        result = await session.execute(select(JobModel).where(JobModel.id == job_uuid))
        job = result.scalar_one_or_none()
        if job is None:
            raise ValueError(f"Job {job_id} not found in database")
        return job

    @classmethod
    def _new_from_job_record(cls, job_id: str, job: JobModel) -> "KnowledgeState":
        """Create a KnowledgeState instance seeded from jobs table metadata."""
        ks = cls.__new__(cls)
        ks.data = {
            "config": {
                "job_id": job_id,
                "research_question": job.research_question,
                "max_iterations": job.max_iterations,
                "use_skills": True,
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

    @classmethod
    async def _load_hypotheses(
        cls, session: AsyncSession, job_uuid: UUID, ks: "KnowledgeState"
    ) -> None:
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

    @classmethod
    async def _load_findings(
        cls, session: AsyncSession, job_uuid: UUID, ks: "KnowledgeState"
    ) -> None:
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

    @classmethod
    async def _load_literature(
        cls, session: AsyncSession, job_uuid: UUID, ks: "KnowledgeState"
    ) -> None:
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

    @classmethod
    async def _load_analysis_log(
        cls, session: AsyncSession, job_uuid: UUID, ks: "KnowledgeState"
    ) -> None:
        """Load analysis log rows into knowledge state format."""
        result = await session.execute(
            select(AnalysisLog)
            .where(AnalysisLog.job_id == job_uuid)
            .order_by(AnalysisLog.created_at)
        )
        for log_entry in result.scalars().all():
            serialized: dict[str, Any] = {
                "iteration": log_entry.iteration,
                "action": log_entry.action_type,
                "timestamp": log_entry.created_at.isoformat(),
                "description": log_entry.description,
                "success": bool(log_entry.success),
            }
            if log_entry.duration_seconds is not None:
                serialized["execution_time"] = log_entry.duration_seconds
            if log_entry.input_data:
                serialized.update(log_entry.input_data)
            if log_entry.output_data:
                serialized.update(log_entry.output_data)
            ks.data["analysis_log"].append(serialized)

    @classmethod
    async def _load_iteration_summaries(
        cls, session: AsyncSession, job_uuid: UUID, ks: "KnowledgeState"
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

    @classmethod
    async def _load_feedback_history(
        cls, session: AsyncSession, job_uuid: UUID, ks: "KnowledgeState"
    ) -> None:
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

    def save_to_database_sync(self, job_id: str, open_session: SessionOpener | None = None) -> None:
        """Run save_to_database in ``open_session``, opened inside the wrapper's own loop."""
        from openscientist.async_tasks import run_sync

        async def save() -> None:
            if open_session is None:
                await self.save_to_database(job_id)
                return
            async with open_session() as session:
                await self.save_to_database(job_id, session)

        run_sync(save())

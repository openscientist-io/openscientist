"""
Knowledge state management for OpenScientist.

Stores agent's state including hypotheses, findings, literature, and analysis history.
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
from openscientist.database.rls import set_current_user
from openscientist.database.session import AsyncSessionLocal

KS_FILENAME = "knowledge_state.json"


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
    """
    Database-backed knowledge state for storing agent state.

    Structure:
        - config: Job configuration
        - data_summary: Overview of uploaded data
        - iteration: Current iteration number
        - hypotheses: List of proposed and tested hypotheses
        - findings: Confirmed discoveries
        - literature: Retrieved papers from PubMed
        - analysis_log: History of all executed analyses
    """

    def __init__(
        self,
        job_id: str,
        research_question: str,
        max_iterations: int,
        use_skills: bool = True,
    ):
        """
        Initialize a new knowledge graph.

        Args:
            job_id: Unique job identifier
            research_question: User's research question
            max_iterations: Maximum number of iterations
            use_skills: Whether skills are enabled for this job
        """
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
        """
        Add a new hypothesis.

        Args:
            statement: Hypothesis statement
            proposed_by: Who proposed it (agent, user, skill)

        Returns:
            hypothesis_id: Unique ID for this hypothesis
        """
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
        """
        Add a confirmed finding.

        Args:
            title: Finding title
            evidence: Statistical evidence (p-values, effect sizes, etc.)
            supporting_hypotheses: List of hypothesis IDs
            literature_support: List of literature IDs
            plots: List of plot file paths
            citations: List of citation dicts with keys pmid, snippet, explanation.
                Each citation is validated against stored abstracts.

        Returns:
            finding_id: Unique ID for this finding
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

        Looks up the PMID in the literature list and checks whether the
        snippet appears in the abstract.  Sets ``validation_status`` to one of:

        - ``verified`` — exact substring match
        - ``verified_normalized`` — match after lowercasing + whitespace collapse
        - ``mismatch`` — snippet not found in abstract
        - ``unchecked`` — PMID not in literature list

        Args:
            citation: Dict with at least ``pmid`` and ``snippet``.

        Returns:
            A new citation dict with ``validation_status`` added.
        """
        import re

        result = dict(citation)
        pmid = result.get("pmid", "")
        snippet = result.get("snippet", "")

        # Find the paper by PMID
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
            # Normalize: lowercase + collapse whitespace + strip punctuation
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
        """
        Add literature reference.

        Args:
            pmid: PubMed ID
            title: Paper title
            abstract: Paper abstract
            relevance_to: List of finding/hypothesis IDs this relates to
            search_query: Query that retrieved this paper

        Returns:
            literature_id: Unique ID for this reference
        """
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
        """
        Log an analysis action.

        Args:
            action: Type of action (execute_code, search_pubmed, use_skill)
            code: Python code executed (if applicable)
            output: Output from execution
            **kwargs: Additional metadata
        """
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
        """
        Store agent-generated summary for an iteration.

        Args:
            iteration: Iteration number
            summary: Plain-language summary of what was accomplished
            strapline: Short, punchy title for this iteration (5-10 words)
        """
        # Ensure iteration_summaries exists (backwards compatibility)
        if "iteration_summaries" not in self.data:
            self.data["iteration_summaries"] = []

        # Check if this iteration already has a summary
        for existing in self.data["iteration_summaries"]:
            if existing["iteration"] == iteration:
                existing["summary"] = summary
                existing["strapline"] = strapline
                existing["updated_at"] = datetime.now(UTC).isoformat()
                return

        # Add new summary
        self.data["iteration_summaries"].append(
            {
                "iteration": iteration,
                "summary": summary,
                "strapline": strapline,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    def set_agent_status(self, status: str) -> None:
        """
        Set the current agent status message.

        This is displayed in the UI to show what the agent is currently doing.
        The agent should call this with a brief, human-readable description
        of its current activity (e.g., "Searching for caffeine-ADHD studies").

        Args:
            status: Brief status message (recommended: under 80 chars)
        """
        self.data["agent_status"] = status
        self.data["agent_status_updated_at"] = datetime.now(UTC).isoformat()

    def clear_agent_status(self) -> None:
        """Clear the agent status (e.g., when job completes)."""
        self.data["agent_status"] = None
        self.data["agent_status_updated_at"] = None

    def get_agent_status(self) -> str | None:
        """Get the current agent status message."""
        return self.data.get("agent_status")

    def get_iteration_summary(self, iteration: int) -> str | None:
        """
        Get the summary for a specific iteration.

        Args:
            iteration: Iteration number

        Returns:
            Summary string or None if not found
        """
        summaries = self.data.get("iteration_summaries", [])
        for entry in summaries:
            if entry["iteration"] == iteration:
                return str(entry["summary"])
        return None

    def increment_iteration(self) -> None:
        """Increment the iteration counter."""
        self.data["iteration"] += 1

    def add_feedback(self, text: str, after_iteration: int) -> None:
        """
        Add scientist feedback to history.

        Feedback is given after an iteration completes and will be injected
        into the next iteration (N+1).

        Args:
            text: Feedback text from scientist
            after_iteration: The iteration that just completed
        """
        # Ensure field exists (backwards compatibility)
        if "feedback_history" not in self.data:
            self.data["feedback_history"] = []

        self.data["feedback_history"].append(
            {
                "after_iteration": after_iteration,
                "text": text,
                "submitted_at": datetime.now(UTC).isoformat(),
            }
        )

    def get_feedback_for_iteration(self, iteration: int) -> str | None:
        """
        Get feedback that should be injected into this iteration.

        Looks for feedback given after the previous iteration (N-1).

        Args:
            iteration: Current iteration number

        Returns:
            Feedback text or None
        """
        feedback_list = self.data.get("feedback_history", [])
        previous_iteration = iteration - 1

        # Find feedback given after the previous iteration
        matching = [f for f in feedback_list if f.get("after_iteration") == previous_iteration]
        if matching:
            return str(matching[-1]["text"])  # Use most recent if multiple
        return None

    def get_summary(self) -> str:
        """
        Get a text summary of current state for prompts.

        Returns:
            Formatted summary of KS state
        """
        summary_parts = [
            f"# Knowledge Graph Summary (Iteration {self.data['iteration']})",
            "",
            "## Research Question",
            self.data["config"]["research_question"],
            "",
            "## Data",
            f"- Files: {self.data['data_summary'].get('files', [])}",
            f"- Samples: {self.data['data_summary'].get('n_samples', 'Unknown')}",
            f"- Features: {self.data['data_summary'].get('n_features', 'Unknown')}",
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

        # Active hypotheses
        pending = [h for h in self.data["hypotheses"] if h["status"] == "pending"]
        if pending:
            summary_parts.append("## Pending Hypotheses")
            summary_parts.extend(f"- {hyp['id']}: {hyp['statement']}" for hyp in pending[-3:])
            summary_parts.append("")

        # Rejected hypotheses (learn from failures)
        rejected = [h for h in self.data["hypotheses"] if h["status"] == "rejected"]
        if rejected:
            summary_parts.append("## Rejected Hypotheses (avoid repeating)")
            summary_parts.extend(
                f"- {hyp['id']}: {hyp['statement']} - {hyp.get('result', {}).get('conclusion', 'No conclusion')}"
                for hyp in rejected[-3:]
            )
            summary_parts.append("")

        return "\n".join(summary_parts)

    def get_report_summary(self) -> str:
        """
        Get a comprehensive summary of all accumulated knowledge for report generation.

        Unlike get_summary() which is concise for iteration prompts, this includes
        ALL findings, hypotheses, literature, and iteration timelines so the report
        agent can write a thorough final report.

        Returns:
            Formatted comprehensive summary of KS state
        """
        all_hypotheses = self.data["hypotheses"]
        supported, rejected, pending = self._split_hypotheses_by_status(all_hypotheses)

        parts = self._report_intro_section()
        self._append_data_summary_section(parts)
        self._append_progress_overview_section(parts, all_hypotheses, supported, rejected, pending)
        self._append_investigation_timeline_section(parts)
        self._append_findings_section(parts)
        self._append_supported_hypotheses_section(parts, supported)
        self._append_rejected_hypotheses_section(parts, rejected)
        self._append_pending_hypotheses_section(parts, pending)
        self._append_literature_section(parts)
        self._append_consensus_answer_section(parts)
        return "\n".join(parts)

    @staticmethod
    def _split_hypotheses_by_status(
        all_hypotheses: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Split hypotheses into supported, rejected, and pending groups."""
        supported = [hyp for hyp in all_hypotheses if hyp["status"] == "supported"]
        rejected = [hyp for hyp in all_hypotheses if hyp["status"] == "rejected"]
        pending = [hyp for hyp in all_hypotheses if hyp["status"] == "pending"]
        return supported, rejected, pending

    def _report_intro_section(self) -> list[str]:
        """Build the report summary header section."""
        return [
            f"# Comprehensive Knowledge Summary (After {self.data['iteration']} iterations)",
            "",
            "## Research Question",
            self.data["config"]["research_question"],
            "",
        ]

    def _append_data_summary_section(self, parts: list[str]) -> None:
        """Append data summary section when available."""
        data_summary = self.data.get("data_summary", {})
        if not data_summary:
            return
        parts.append("## Data Summary")
        parts.append(f"- Files: {data_summary.get('files', [])}")
        parts.append(f"- Samples: {data_summary.get('n_samples', 'Unknown')}")
        parts.append(f"- Features: {data_summary.get('n_features', 'Unknown')}")
        parts.append("")

    def _append_progress_overview_section(
        self,
        parts: list[str],
        all_hypotheses: list[dict[str, Any]],
        supported: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        pending: list[dict[str, Any]],
    ) -> None:
        """Append high-level counts covering hypotheses/findings/literature."""
        parts.append("## Progress Overview")
        parts.append(f"- Total hypotheses proposed: {len(all_hypotheses)}")
        parts.append(f"- Supported: {len(supported)}")
        parts.append(f"- Rejected: {len(rejected)}")
        parts.append(f"- Pending: {len(pending)}")
        parts.append(f"- Findings confirmed: {len(self.data['findings'])}")
        parts.append(f"- Literature reviewed: {len(self.data['literature'])}")
        parts.append("")

    def _append_investigation_timeline_section(self, parts: list[str]) -> None:
        """Append ordered iteration summaries."""
        summaries = self.data.get("iteration_summaries", [])
        if not summaries:
            return
        parts.append("## Investigation Timeline")
        for entry in sorted(summaries, key=lambda item: item["iteration"]):
            strapline = entry.get("strapline", "")
            label = f" — {strapline}" if strapline else ""
            parts.append(f"- **Iteration {entry['iteration']}{label}:** {entry['summary']}")
        parts.append("")

    def _append_findings_section(self, parts: list[str]) -> None:
        """Append all findings with evidence and optional metadata."""
        findings = self.data["findings"]
        if not findings:
            return
        parts.append("## All Findings")
        for finding in findings:
            parts.append(f"### {finding['id']}: {finding['title']}")
            parts.append(f"- **Evidence:** {finding['evidence']}")
            if finding.get("biological_interpretation"):
                parts.append(f"- **Interpretation:** {finding['biological_interpretation']}")
            if finding.get("supporting_hypotheses"):
                parts.append(
                    f"- **Supporting hypotheses:** {', '.join(finding['supporting_hypotheses'])}"
                )
            if finding.get("literature_support"):
                parts.append(
                    f"- **Literature support:** {', '.join(finding['literature_support'])}"
                )
            if finding.get("plots"):
                parts.append(f"- **Plots:** {', '.join(finding['plots'])}")
            parts.append("")

    @staticmethod
    def _append_hypothesis_result_details(parts: list[str], result: dict[str, Any]) -> None:
        """Append optional hypothesis result fields."""
        if result.get("summary"):
            parts.append(f"- **Result:** {result['summary']}")
        if result.get("p_value"):
            parts.append(f"- **P-value:** {result['p_value']}")
        if result.get("effect_size"):
            parts.append(f"- **Effect size:** {result['effect_size']}")
        if result.get("conclusion"):
            parts.append(f"- **Conclusion:** {result['conclusion']}")

    def _append_supported_hypotheses_section(
        self, parts: list[str], supported: list[dict[str, Any]]
    ) -> None:
        """Append all supported hypotheses and measured outcomes."""
        if not supported:
            return
        parts.append("## Supported Hypotheses")
        for hypothesis in supported:
            parts.append(f"### {hypothesis['id']}: {hypothesis['statement']}")
            self._append_hypothesis_result_details(parts, hypothesis.get("result") or {})
            parts.append("")

    def _append_rejected_hypotheses_section(
        self, parts: list[str], rejected: list[dict[str, Any]]
    ) -> None:
        """Append rejected hypotheses with conclusions."""
        if not rejected:
            return
        parts.append("## Rejected Hypotheses")
        for hypothesis in rejected:
            parts.append(f"### {hypothesis['id']}: {hypothesis['statement']}")
            result = hypothesis.get("result") or {}
            if result.get("conclusion"):
                parts.append(f"- **Conclusion:** {result['conclusion']}")
            elif result.get("summary"):
                parts.append(f"- **Result:** {result['summary']}")
            parts.append("")

    @staticmethod
    def _append_pending_hypotheses_section(parts: list[str], pending: list[dict[str, Any]]) -> None:
        """Append still-pending hypotheses as remaining knowledge gaps."""
        if not pending:
            return
        parts.append("## Knowledge Gaps (Pending Hypotheses)")
        parts.extend(f"- {hypothesis['id']}: {hypothesis['statement']}" for hypothesis in pending)
        parts.append("")

    def _append_literature_section(self, parts: list[str]) -> None:
        """Append reviewed literature titles and compact abstracts."""
        literature_entries = self.data["literature"]
        if not literature_entries:
            return
        parts.append("## Literature Reviewed")
        for literature in literature_entries:
            pmid_str = f" (PMID: {literature['pmid']})" if literature.get("pmid") else ""
            parts.append(f"- **{literature['title']}**{pmid_str}")
            abstract = literature.get("abstract", "")
            if abstract:
                truncated = abstract[:200] + "..." if len(abstract) > 200 else abstract
                parts.append(f"  Abstract: {truncated}")
            if literature.get("relevance_to"):
                parts.append(f"  Relevant to: {', '.join(literature['relevance_to'])}")
        parts.append("")

    def _append_consensus_answer_section(self, parts: list[str]) -> None:
        """Append consensus answer when one has been produced."""
        consensus_answer = self.data.get("consensus_answer")
        if not consensus_answer:
            return
        parts.append("## Previous Consensus Answer")
        parts.append(consensus_answer)
        parts.append("")

    def get_report_outline(self) -> str:
        """Get an outline of accumulated knowledge for the report prompt.

        Includes finding titles, hypothesis outcomes, iteration straplines,
        and literature entries with full abstracts for citation grounding.
        """
        parts: list[str] = [
            f"# Knowledge Outline ({self.data['iteration']} iterations completed)",
            "",
            "## Research Question",
            self.data["config"]["research_question"],
            "",
        ]

        # Progress counts
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

        # Findings — titles with citations when available
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

        # Hypotheses — one-line status
        if all_hyps:
            parts.append("## Hypotheses")
            parts.extend(f"- {hyp['id']} [{hyp['status']}]: {hyp['statement']}" for hyp in all_hyps)
            parts.append("")

        # Literature — titles with abstracts for citation grounding.
        # Papers already cited by findings get title+PMID only (the snippet
        # is the grounding); uncited papers include full abstracts as fallback.
        cited_pmids: set[str] = set()
        for finding in self.data["findings"]:
            for c in finding.get("citations", []):
                if c.get("pmid"):
                    cited_pmids.add(str(c["pmid"]))

        if self.data["literature"]:
            parts.append(f"## Literature ({len(self.data['literature'])} papers)")
            for lit in self.data["literature"]:
                pmid_str = f" (PMID: {lit['pmid']})" if lit.get("pmid") else ""
                parts.append(f"- **{lit['title']}**{pmid_str}")
                # Only include abstracts for papers not already cited by findings
                if str(lit.get("pmid", "")) not in cited_pmids:
                    abstract = lit.get("abstract", "")
                    if abstract:
                        parts.append(f"  Abstract: {abstract}")
            parts.append("")

        # Consensus answer if exists
        if self.data.get("consensus_answer"):
            parts.append("## Current Consensus Answer")
            parts.append(self.data["consensus_answer"])
            parts.append("")

        return "\n".join(parts)

    def set_version_info(self, version_info: dict[str, str]) -> None:
        """
        Set version/environment metadata in config.

        Args:
            version_info: Dict with keys like 'claude_model', 'claude_code_version',
                         'openscientist_commit', 'docker_image_id'
        """
        self.data["config"]["version_info"] = version_info

    def to_dict(self) -> dict[str, Any]:
        """Return the raw data dictionary."""
        return self.data

    # Database persistence methods

    async def _save_to_database_with_session(
        self,
        session: AsyncSession,
        job_id: str,
        user_id: UUID | None = None,
    ) -> None:
        """Persist knowledge-state rows using an existing session."""
        if user_id:
            await set_current_user(session, user_id)

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

    async def save_to_database(
        self,
        job_id: str,
        user_id: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """
        Save knowledge state to database tables.

        This persists hypotheses, findings, literature, analysis_log, iteration_summaries,
        and feedback_history to their respective tables.

        Args:
            job_id: Job ID (UUID string)
            user_id: User ID for RLS context (optional)
        """
        if session is None:
            async with AsyncSessionLocal(thread_safe=True) as session:
                await self._save_to_database_with_session(session, job_id, user_id)
                await session.commit()
            return

        await self._save_to_database_with_session(session, job_id, user_id)
        await session.commit()

    @classmethod
    async def load_from_database(cls, job_id: str, user_id: UUID | None = None) -> "KnowledgeState":
        """
        Load knowledge state from database tables.

        Args:
            job_id: Job ID (UUID string)
            user_id: User ID for RLS context (optional)

        Returns:
            KnowledgeState instance populated from database
        """
        async with AsyncSessionLocal(thread_safe=True) as session:
            if user_id:
                await set_current_user(session, user_id)

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
                "research_question": job.title,
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

    def save_to_database_sync(self, job_id: str, user_id: UUID | None = None) -> None:
        """Synchronous wrapper for save_to_database."""
        from openscientist.async_tasks import run_sync

        run_sync(self.save_to_database(job_id, user_id))

    @classmethod
    def load_from_database_sync(cls, job_id: str, user_id: UUID | None = None) -> "KnowledgeState":
        """Synchronous wrapper for load_from_database."""
        from openscientist.async_tasks import run_sync

        return run_sync(cls.load_from_database(job_id, user_id))

"""
Knowledge state management for SHANDY.

Stores agent's state including hypotheses, findings, literature, and analysis history.
"""

import asyncio
import fcntl
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import select

from shandy.database.models.analysis_log import AnalysisLog
from shandy.database.models.feedback_history import FeedbackHistory
from shandy.database.models.finding import Finding
from shandy.database.models.hypothesis import Hypothesis
from shandy.database.models.iteration_summary import IterationSummary
from shandy.database.models.job import Job as JobModel
from shandy.database.models.literature import Literature
from shandy.database.rls import set_current_user
from shandy.database.session import AsyncSessionLocal


class KnowledgeState:
    """
    JSON-based knowledge state for storing agent state.

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
        self.data: Dict[str, Any] = {
            "config": {
                "job_id": job_id,
                "research_question": research_question,
                "max_iterations": max_iterations,
                "use_skills": use_skills,
                "started_at": datetime.now().isoformat(),
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

    @classmethod
    def load(cls, file_path: Path) -> "KnowledgeState":
        """Load knowledge graph from JSON file with shared lock."""
        with open(file_path, "r", encoding="utf-8") as f:
            # Acquire shared lock - blocks while exclusive lock is held
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        # Create instance and set data
        ks = cls.__new__(cls)
        ks.data = data
        return ks

    def save(self, file_path: Path) -> None:
        """Save knowledge graph to JSON file with file locking."""
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Create file if it doesn't exist
        if not file_path.exists():
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            return

        # Open for read-write and acquire exclusive lock
        with open(file_path, "r+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                # Write the data
                f.seek(0)
                f.truncate()
                json.dump(self.data, f, indent=2)
            finally:
                # Release lock
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def set_data_summary(self, summary: Dict[str, Any]) -> None:
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

    def update_hypothesis(self, hypothesis_id: str, updates: Dict[str, Any]) -> None:
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
        supporting_hypotheses: Optional[List[str]] = None,
        literature_support: Optional[List[str]] = None,
        plots: Optional[List[str]] = None,
    ) -> str:
        """
        Add a confirmed finding.

        Args:
            title: Finding title
            evidence: Statistical evidence (p-values, effect sizes, etc.)
            supporting_hypotheses: List of hypothesis IDs
            literature_support: List of literature IDs
            plots: List of plot file paths

        Returns:
            finding_id: Unique ID for this finding
        """
        finding_id = f"F{len(self.data['findings']) + 1:03d}"

        finding = {
            "id": finding_id,
            "iteration_discovered": self.data["iteration"],
            "title": title,
            "evidence": evidence,
            "supporting_hypotheses": supporting_hypotheses or [],
            "literature_support": literature_support or [],
            "plots": plots or [],
            "biological_interpretation": "",
        }

        self.data["findings"].append(finding)
        return finding_id

    def add_literature(
        self,
        pmid: str,
        title: str,
        abstract: str,
        relevance_to: Optional[List[str]] = None,
        search_query: Optional[str] = None,
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
        code: Optional[str] = None,
        output: Optional[str] = None,
        **kwargs,
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
            "timestamp": datetime.now().isoformat(),
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
                existing["updated_at"] = datetime.now().isoformat()
                return

        # Add new summary
        self.data["iteration_summaries"].append(
            {
                "iteration": iteration,
                "summary": summary,
                "strapline": strapline,
                "created_at": datetime.now().isoformat(),
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
        self.data["agent_status_updated_at"] = datetime.now().isoformat()

    def clear_agent_status(self) -> None:
        """Clear the agent status (e.g., when job completes)."""
        self.data["agent_status"] = None
        self.data["agent_status_updated_at"] = None

    def get_agent_status(self) -> Optional[str]:
        """Get the current agent status message."""
        return self.data.get("agent_status")

    def get_iteration_summary(self, iteration: int) -> Optional[str]:
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
                "submitted_at": datetime.now().isoformat(),
            }
        )

    def get_feedback_for_iteration(self, iteration: int) -> Optional[str]:
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
            for finding in self.data["findings"][-3:]:
                summary_parts.append(f"- **{finding['title']}**: {finding['evidence']}")
            summary_parts.append("")

        # Active hypotheses
        pending = [h for h in self.data["hypotheses"] if h["status"] == "pending"]
        if pending:
            summary_parts.append("## Pending Hypotheses")
            for hyp in pending[-3:]:
                summary_parts.append(f"- {hyp['id']}: {hyp['statement']}")
            summary_parts.append("")

        # Rejected hypotheses (learn from failures)
        rejected = [h for h in self.data["hypotheses"] if h["status"] == "rejected"]
        if rejected:
            summary_parts.append("## Rejected Hypotheses (avoid repeating)")
            for hyp in rejected[-3:]:
                summary_parts.append(
                    f"- {hyp['id']}: {hyp['statement']} - {hyp.get('result', {}).get('conclusion', 'No conclusion')}"
                )
            summary_parts.append("")

        return "\n".join(summary_parts)

    def set_version_info(self, version_info: Dict[str, str]) -> None:
        """
        Set version/environment metadata in config.

        Args:
            version_info: Dict with keys like 'claude_model', 'claude_code_version',
                         'shandy_commit', 'docker_image_id'
        """
        self.data["config"]["version_info"] = version_info

    def to_dict(self) -> Dict[str, Any]:
        """Return the raw data dictionary."""
        return self.data

    # Database persistence methods

    async def save_to_database(self, job_id: str, user_id: Optional[UUID] = None) -> None:
        """
        Save knowledge state to database tables.

        This persists hypotheses, findings, literature, analysis_log, iteration_summaries,
        and feedback_history to their respective tables.

        Args:
            job_id: Job ID (UUID string)
            user_id: User ID for RLS context (optional)
        """
        async with AsyncSessionLocal(thread_safe=True) as session:
            if user_id:
                await set_current_user(session, user_id)

            job_uuid = UUID(job_id)

            # Update job's current iteration and consensus answer
            job_stmt = select(JobModel).where(JobModel.id == job_uuid)
            job_result = await session.execute(job_stmt)
            job = job_result.scalar_one_or_none()
            if job:
                job.current_iteration = self.data["iteration"]
                # Save consensus answer if available
                if self.data.get("consensus_answer"):
                    job.consensus_answer = self.data["consensus_answer"]

            # Save hypotheses
            for hyp_data in self.data["hypotheses"]:
                # Check if hypothesis already exists (by looking for text match)
                # Use .first() in case of duplicate statements
                hyp_stmt = select(Hypothesis).where(
                    Hypothesis.job_id == job_uuid,
                    Hypothesis.text == hyp_data["statement"],
                )
                hyp_result = await session.execute(hyp_stmt)
                existing_hyp = hyp_result.scalars().first()

                if not existing_hyp:
                    hypothesis = Hypothesis(
                        job_id=job_uuid,
                        iteration=hyp_data["iteration_proposed"],
                        text=hyp_data["statement"],
                        status=hyp_data["status"],
                        confidence=None,
                        priority=None,
                        rationale=f"Proposed by {hyp_data.get('proposed_by', 'agent')}",
                        test_strategy=hyp_data.get("test_code"),
                        supporting_evidence={"result": hyp_data.get("result")},
                    )
                    session.add(hypothesis)
                else:
                    # Update existing
                    existing_hyp.status = hyp_data["status"]
                    existing_hyp.test_strategy = hyp_data.get("test_code")
                    existing_hyp.supporting_evidence = {"result": hyp_data.get("result")}

            # Save findings
            for finding_data in self.data["findings"]:
                # Check if finding already exists
                # Use .first() in case of duplicate titles
                finding_stmt = select(Finding).where(
                    Finding.job_id == job_uuid, Finding.text == finding_data["title"]
                )
                finding_result = await session.execute(finding_stmt)
                existing_finding = finding_result.scalars().first()

                if not existing_finding:
                    finding = Finding(
                        job_id=job_uuid,
                        iteration=finding_data["iteration_discovered"],
                        text=finding_data["title"],
                        finding_type="observation",
                        source="code_execution",
                        data={
                            "evidence": finding_data["evidence"],
                            "supporting_hypotheses": finding_data.get("supporting_hypotheses", []),
                            "literature_support": finding_data.get("literature_support", []),
                            "plots": finding_data.get("plots", []),
                            "biological_interpretation": finding_data.get(
                                "biological_interpretation", ""
                            ),
                        },
                    )
                    session.add(finding)

            # Save literature
            for lit_data in self.data["literature"]:
                # Check if literature already exists (by title match since no pmid field)
                # Use .first() since the same paper might appear multiple times
                lit_stmt = select(Literature).where(
                    Literature.job_id == job_uuid, Literature.title == lit_data["title"]
                )
                lit_result = await session.execute(lit_stmt)
                existing_lit = lit_result.scalars().first()

                if not existing_lit:
                    lit_record = Literature(
                        job_id=job_uuid,
                        iteration=lit_data.get("retrieved_at_iteration", 1),
                        title=lit_data["title"],
                        abstract=lit_data.get("abstract"),
                        authors=None,
                        journal=None,
                        year=None,
                        doi=None,
                        extra_metadata={
                            "pmid": lit_data.get("pmid"),
                            "search_query": lit_data.get("search_query"),
                        },
                    )
                    session.add(lit_record)

            # Save analysis log entries
            for log_entry in self.data["analysis_log"]:
                # Check if log entry already exists (by iteration + description + timestamp)
                # Use .first() since the created_at >= check may match multiple rows
                log_stmt = select(AnalysisLog).where(
                    AnalysisLog.job_id == job_uuid,
                    AnalysisLog.iteration == log_entry["iteration"],
                    AnalysisLog.description == log_entry.get("action", ""),
                    AnalysisLog.created_at >= datetime.fromisoformat(log_entry["timestamp"]),
                )
                log_result = await session.execute(log_stmt)
                existing_log = log_result.scalars().first()

                if not existing_log:
                    analysis_log = AnalysisLog(
                        job_id=job_uuid,
                        iteration=log_entry["iteration"],
                        step_number=1,
                        action_type=log_entry["action"],
                        description=log_entry.get("action", ""),
                        input_data=(
                            {"code": log_entry.get("code")} if log_entry.get("code") else None
                        ),
                        output_data=(
                            {"output": log_entry.get("output")} if log_entry.get("output") else None
                        ),
                        success=True,
                    )
                    session.add(analysis_log)

            # Save iteration summaries
            for summary_data in self.data.get("iteration_summaries", []):
                # Check if summary already exists
                # Use .first() in case iteration constraint isn't enforced
                summ_stmt = select(IterationSummary).where(
                    IterationSummary.job_id == job_uuid,
                    IterationSummary.iteration == summary_data["iteration"],
                )
                summ_result = await session.execute(summ_stmt)
                existing_summ = summ_result.scalars().first()

                if not existing_summ:
                    iteration_summary = IterationSummary(
                        job_id=job_uuid,
                        iteration=summary_data["iteration"],
                        summary_text=summary_data["summary"],
                    )
                    session.add(iteration_summary)
                else:
                    existing_summ.summary_text = summary_data["summary"]

            # Save feedback history
            for feedback_data in self.data.get("feedback_history", []):
                # Check if feedback already exists
                # Use .first() to handle potential duplicates
                fb_stmt = select(FeedbackHistory).where(
                    FeedbackHistory.job_id == job_uuid,
                    FeedbackHistory.iteration == feedback_data["after_iteration"],
                )
                fb_result = await session.execute(fb_stmt)
                existing_fb = fb_result.scalars().first()

                if not existing_fb:
                    feedback = FeedbackHistory(
                        job_id=job_uuid,
                        iteration=feedback_data["after_iteration"],
                        feedback_type="user_feedback",
                        feedback_text=feedback_data["text"],
                    )
                    session.add(feedback)

            await session.commit()

    @classmethod
    async def load_from_database(
        cls, job_id: str, user_id: Optional[UUID] = None
    ) -> "KnowledgeState":
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

            # Load job
            job_stmt = select(JobModel).where(JobModel.id == job_uuid)
            job_result = await session.execute(job_stmt)
            job = job_result.scalar_one_or_none()

            if not job:
                raise ValueError(f"Job {job_id} not found in database")

            # Create new KnowledgeState instance
            ks = cls.__new__(cls)
            ks.data = {
                "config": {
                    "job_id": job_id,
                    "research_question": job.title,
                    "max_iterations": job.max_iterations,
                    "use_skills": True,  # Default
                    "started_at": job.created_at.isoformat(),
                },
                "data_summary": {},  # Would need to load from job_data_files
                "iteration": job.current_iteration,
                "hypotheses": [],
                "findings": [],
                "literature": [],
                "analysis_log": [],
                "iteration_summaries": [],
                "feedback_history": [],
            }

            # Load hypotheses
            hyp_stmt = (
                select(Hypothesis)
                .where(Hypothesis.job_id == job_uuid)
                .order_by(Hypothesis.created_at)
            )
            hyp_result = await session.execute(hyp_stmt)
            hypotheses = hyp_result.scalars().all()

            for hyp in hypotheses:
                ks.data["hypotheses"].append(
                    {
                        "id": f"H{len(ks.data['hypotheses']) + 1:03d}",
                        "iteration_proposed": hyp.iteration,
                        "statement": hyp.text,
                        "status": hyp.status,
                        "proposed_by": "agent",
                        "tested_at_iteration": None,
                        "test_code": hyp.test_strategy,
                        "result": (
                            hyp.supporting_evidence.get("result")
                            if hyp.supporting_evidence
                            else None
                        ),
                        "spawned_hypotheses": [],
                    }
                )

            # Load findings
            finding_stmt = (
                select(Finding).where(Finding.job_id == job_uuid).order_by(Finding.created_at)
            )
            finding_result = await session.execute(finding_stmt)
            findings = finding_result.scalars().all()

            for finding in findings:
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
                        "biological_interpretation": finding_data.get(
                            "biological_interpretation", ""
                        ),
                    }
                )

            # Load literature
            lit_stmt = (
                select(Literature)
                .where(Literature.job_id == job_uuid)
                .order_by(Literature.created_at)
            )
            lit_result = await session.execute(lit_stmt)
            literature_list = lit_result.scalars().all()

            for lit in literature_list:
                extra = lit.extra_metadata or {}
                ks.data["literature"].append(
                    {
                        "id": f"L{len(ks.data['literature']) + 1:03d}",
                        "pmid": extra.get("pmid", ""),
                        "title": lit.title,
                        "abstract": lit.abstract,
                        "relevance_to": [],
                        "retrieved_at_iteration": lit.iteration,
                        "search_query": extra.get("search_query"),
                    }
                )

            # Load analysis log
            log_stmt = (
                select(AnalysisLog)
                .where(AnalysisLog.job_id == job_uuid)
                .order_by(AnalysisLog.created_at)
            )
            log_result = await session.execute(log_stmt)
            logs = log_result.scalars().all()

            for log in logs:
                log_entry: Dict[str, Any] = {
                    "iteration": log.iteration,
                    "action": log.action_type,
                    "timestamp": log.created_at.isoformat(),
                }
                if log.input_data and log.input_data.get("code"):
                    log_entry["code"] = log.input_data["code"]
                if log.output_data and log.output_data.get("output"):
                    log_entry["output"] = log.output_data["output"]
                ks.data["analysis_log"].append(log_entry)

            # Load iteration summaries
            summ_stmt = (
                select(IterationSummary)
                .where(IterationSummary.job_id == job_uuid)
                .order_by(IterationSummary.iteration)
            )
            summ_result = await session.execute(summ_stmt)
            summaries = summ_result.scalars().all()

            for summary in summaries:
                ks.data["iteration_summaries"].append(
                    {
                        "iteration": summary.iteration,
                        "summary": summary.summary_text,
                        "created_at": summary.created_at.isoformat(),
                    }
                )

            # Load feedback history
            fb_stmt = (
                select(FeedbackHistory)
                .where(FeedbackHistory.job_id == job_uuid)
                .order_by(FeedbackHistory.created_at)
            )
            fb_result = await session.execute(fb_stmt)
            feedback_list = fb_result.scalars().all()

            for feedback in feedback_list:
                ks.data["feedback_history"].append(
                    {
                        "after_iteration": feedback.iteration,
                        "text": feedback.feedback_text,
                        "submitted_at": feedback.created_at.isoformat(),
                    }
                )

            return ks

    def save_to_database_sync(self, job_id: str, user_id: Optional[UUID] = None) -> None:
        """Synchronous wrapper for save_to_database."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.save_to_database(job_id, user_id))
        finally:
            loop.close()

    @classmethod
    def load_from_database_sync(
        cls, job_id: str, user_id: Optional[UUID] = None
    ) -> "KnowledgeState":
        """Synchronous wrapper for load_from_database."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(cls.load_from_database(job_id, user_id))
        finally:
            loop.close()

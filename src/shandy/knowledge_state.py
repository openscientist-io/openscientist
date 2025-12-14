"""
Knowledge state management for SHANDY.

Stores agent's state including hypotheses, findings, literature, and analysis history.
"""

import fcntl
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


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

    def __init__(self, job_id: str, research_question: str, max_iterations: int,
                 use_skills: bool = True):
        """
        Initialize a new knowledge graph.

        Args:
            job_id: Unique job identifier
            research_question: User's research question
            max_iterations: Maximum number of iterations
            use_skills: Whether skills are enabled for this job
        """
        self.data = {
            "config": {
                "job_id": job_id,
                "research_question": research_question,
                "max_iterations": max_iterations,
                "use_skills": use_skills,
                "started_at": datetime.now().isoformat()
            },
            "data_summary": {},
            "iteration": 1,
            "hypotheses": [],
            "findings": [],
            "literature": [],
            "analysis_log": [],
            "iteration_summaries": [],  # Agent-generated summaries per iteration
            "feedback_history": []  # Scientist feedback: [{after_iteration, text, submitted_at}]
        }

    @classmethod
    def load(cls, file_path: Path) -> "KnowledgeState":
        """Load knowledge graph from JSON file with shared lock."""
        with open(file_path, 'r') as f:
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
            with open(file_path, 'w') as f:
                json.dump(self.data, f, indent=2)
            return

        # Open for read-write and acquire exclusive lock
        with open(file_path, 'r+') as f:
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
            "spawned_hypotheses": []
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

    def add_finding(self, title: str, evidence: str,
                   supporting_hypotheses: Optional[List[str]] = None,
                   literature_support: Optional[List[str]] = None,
                   plots: Optional[List[str]] = None) -> str:
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
            "biological_interpretation": ""
        }

        self.data["findings"].append(finding)
        return finding_id

    def add_literature(self, pmid: str, title: str, abstract: str,
                      relevance_to: Optional[List[str]] = None,
                      search_query: Optional[str] = None) -> str:
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
            "search_query": search_query
        }

        self.data["literature"].append(literature)
        return literature_id

    def log_analysis(self, action: str, code: Optional[str] = None,
                    output: Optional[str] = None, **kwargs) -> None:
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
            **kwargs
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
        self.data["iteration_summaries"].append({
            "iteration": iteration,
            "summary": summary,
            "strapline": strapline,
            "created_at": datetime.now().isoformat()
        })

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
                return entry["summary"]
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

        self.data["feedback_history"].append({
            "after_iteration": after_iteration,
            "text": text,
            "submitted_at": datetime.now().isoformat()
        })

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
            return matching[-1]["text"]  # Use most recent if multiple
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
            f"## Research Question",
            self.data['config']['research_question'],
            "",
            f"## Data",
            f"- Files: {self.data['data_summary'].get('files', [])}",
            f"- Samples: {self.data['data_summary'].get('n_samples', 'Unknown')}",
            f"- Features: {self.data['data_summary'].get('n_features', 'Unknown')}",
            "",
            f"## Progress",
            f"- Hypotheses tested: {len([h for h in self.data['hypotheses'] if h['status'] != 'pending'])}",
            f"- Findings confirmed: {len(self.data['findings'])}",
            f"- Literature reviewed: {len(self.data['literature'])}",
            ""
        ]

        # Recent findings
        if self.data['findings']:
            summary_parts.append("## Recent Findings")
            for finding in self.data['findings'][-3:]:
                summary_parts.append(f"- **{finding['title']}**: {finding['evidence']}")
            summary_parts.append("")

        # Active hypotheses
        pending = [h for h in self.data['hypotheses'] if h['status'] == 'pending']
        if pending:
            summary_parts.append("## Pending Hypotheses")
            for hyp in pending[-3:]:
                summary_parts.append(f"- {hyp['id']}: {hyp['statement']}")
            summary_parts.append("")

        # Rejected hypotheses (learn from failures)
        rejected = [h for h in self.data['hypotheses'] if h['status'] == 'rejected']
        if rejected:
            summary_parts.append("## Rejected Hypotheses (avoid repeating)")
            for hyp in rejected[-3:]:
                summary_parts.append(f"- {hyp['id']}: {hyp['statement']} - {hyp.get('result', {}).get('conclusion', 'No conclusion')}")
            summary_parts.append("")

        return "\n".join(summary_parts)

    def to_dict(self) -> Dict[str, Any]:
        """Return the raw data dictionary."""
        return self.data

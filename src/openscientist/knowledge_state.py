"""
Knowledge state management for OpenScientist.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.session import SessionOpener

# Bound for the run's facts and the literature abstracts in the report prompt.
# Dumping every abstract overflows the model's context window and drops the tool
# definitions before the file-write call is emitted. Applies only when no budget is given.
_DEFAULT_REPORT_OUTLINE_BUDGET_CHARS = 8000


class KnowledgeState:
    """Database-backed knowledge state for storing agent state."""

    def __init__(self, job_id: str, research_question: str, max_iterations: int):
        """Initialize a new knowledge graph."""
        self.data: dict[str, Any] = {
            "config": {
                "job_id": job_id,
                "research_question": research_question,
                "max_iterations": max_iterations,
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

    def get_report_outline(self, *, budget_chars: int | None = None) -> str:
        """Get an outline of accumulated knowledge for the report prompt.

        Includes the data files, the full iteration summaries, findings with
        their interpretations, hypotheses with their results, and literature
        entries with abstracts for citation grounding. A first report and a
        regeneration read the same facts, since a regeneration starts a fresh
        agent session with this outline as its only source.

        Args:
            budget_chars: Character budget for the run's detailed facts and the
                uncited-paper abstracts. The facts take it first. If they alone
                exceed it, the iteration summaries shrink to their straplines.
                Abstracts fill what remains, each in full, and the rest are
                omitted with a note. The caller derives this from the model's
                context window so the prompt cannot overflow it. Defaults to a
                conservative bound when unset.
        """
        budget = budget_chars if budget_chars is not None else _DEFAULT_REPORT_OUTLINE_BUDGET_CHARS
        data_lines = self._outline_data_lines()
        findings_lines, findings_chars = self._outline_findings_lines()
        hypotheses_lines, hypotheses_chars = self._outline_hypotheses_lines()
        fixed_chars = sum(len(line) for line in data_lines) + findings_chars + hypotheses_chars
        timeline_lines, timeline_chars = self._outline_timeline_lines(full=True)
        if fixed_chars + timeline_chars > budget:
            timeline_lines, timeline_chars = self._outline_timeline_lines(full=False)
        abstract_budget = max(0, budget - fixed_chars - timeline_chars)

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

        parts.extend(data_lines)
        parts.extend(timeline_lines)
        parts.extend(findings_lines)
        parts.extend(hypotheses_lines)

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
                    if abstract_chars_spent + len(abstract) > abstract_budget:
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

    def _outline_data_lines(self) -> list[str]:
        """The data files the job was given, as setup recorded them."""
        summary = self.data.get("data_summary") or {}
        files = summary.get("files") or []
        if not files:
            return []
        lines = ["## Data", f"- Files: {', '.join(str(name) for name in files)}"]
        details = []
        if summary.get("file_type"):
            details.append(str(summary["file_type"]))
        size = summary.get("file_size_mb")
        if isinstance(size, int | float):
            details.append(f"{size:.1f} MB")
        if details:
            lines.append(f"- First file: {', '.join(details)}")
        lines.append("")
        return lines

    def _outline_timeline_lines(self, *, full: bool) -> tuple[list[str], int]:
        """Each iteration's strapline, with its full summary when ``full``, and
        the characters the summaries add."""
        summaries = self.data.get("iteration_summaries", [])
        if not summaries:
            return [], 0
        lines = ["## Investigation Timeline"]
        added = 0
        for entry in sorted(summaries, key=lambda e: e["iteration"]):
            summary = str(entry.get("summary") or "")
            strapline = entry.get("strapline") or summary[:120]
            lines.append(f"- Iteration {entry['iteration']}: {strapline}")
            if full and summary and summary != strapline:
                lines.append(f"  {summary}")
                added += len(summary)
        lines.append("")
        return lines, added

    def _outline_findings_lines(self) -> tuple[list[str], int]:
        """Findings with their evidence, interpretation and citations, and the
        characters the interpretations add."""
        if not self.data["findings"]:
            return [], 0
        lines = ["## Findings"]
        added = 0
        for finding in self.data["findings"]:
            lines.append(f"- {finding['id']}: {finding['title']}")
            if finding.get("evidence"):
                lines.append(f"  Statistical evidence: {finding['evidence']}")
            interpretation = finding.get("biological_interpretation")
            if interpretation:
                lines.append(f"  Interpretation: {interpretation}")
                added += len(interpretation)
            for c in finding.get("citations", []):
                status = c.get("validation_status", "unchecked")
                pmid = c.get("pmid", "?")
                snippet = c.get("snippet", "")
                explanation = c.get("explanation", "")
                lines.append(f'  - PMID:{pmid} [{status}]: "{snippet}"')
                if explanation:
                    lines.append(f"    → {explanation}")
        lines.append("")
        return lines, added

    def _outline_hypotheses_lines(self) -> tuple[list[str], int]:
        """Hypotheses with their status and test result, and the characters the
        results add."""
        if not self.data["hypotheses"]:
            return [], 0
        lines = ["## Hypotheses"]
        added = 0
        for hyp in self.data["hypotheses"]:
            lines.append(f"- {hyp['id']} [{hyp['status']}]: {hyp['statement']}")
            result = hyp.get("result")
            if not isinstance(result, dict):
                continue
            result_lines = []
            if result.get("summary"):
                result_lines.append(f"  Result: {result['summary']}")
            statistics = [
                f"{label} {result[key]}"
                for key, label in (("p_value", "p ="), ("effect_size", "effect size"))
                if result.get(key) not in (None, "")
            ]
            if statistics:
                result_lines.append(f"  Statistics: {', '.join(statistics)}")
            if result.get("conclusion"):
                result_lines.append(f"  Conclusion: {result['conclusion']}")
            lines.extend(result_lines)
            added += sum(len(line) for line in result_lines)
        lines.append("")
        return lines, added

    def to_dict(self) -> dict[str, Any]:
        """Return the raw data dictionary."""
        return self.data

    # knowledge_state_db imports this module, so each persistence method
    # imports it inside the body to avoid a circular import.

    async def save_to_database(self, job_id: str, session: AsyncSession | None = None) -> None:
        """Save knowledge state to database tables."""
        from openscientist.knowledge_state_db import save_to_database

        await save_to_database(self, job_id, session)

    @classmethod
    async def load_from_database(
        cls, job_id: str, session: AsyncSession | None = None
    ) -> "KnowledgeState":
        """Load knowledge state from database tables."""
        from openscientist.knowledge_state_db import load_from_database

        return await load_from_database(job_id, session)

    def save_to_database_sync(self, job_id: str, open_session: SessionOpener | None = None) -> None:
        """Run save_to_database in ``open_session``, opened inside the wrapper's own loop."""
        from openscientist.knowledge_state_db import save_to_database_sync

        save_to_database_sync(self, job_id, open_session)

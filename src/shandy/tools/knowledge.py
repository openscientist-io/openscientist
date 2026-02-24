"""
Knowledge state tools for the SDK agent path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from shandy.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)


def make_tools(ctx: ToolContext, use_hypotheses: bool = False) -> list[Callable[..., Any]]:
    """Return knowledge state tools bound to ctx."""

    @tool
    def update_knowledge_state(
        title: str, evidence: str, interpretation: str = "", description: str = ""
    ) -> str:
        """
        Record a confirmed finding to the knowledge graph.

        Args:
            title: Finding title (concise description)
            evidence: Statistical evidence (p-values, effect sizes, etc.)
            interpretation: Biological/mechanistic interpretation (optional)
            description: Why you're recording this finding

        Returns:
            Confirmation with finding number
        """
        from shandy.knowledge_state import KnowledgeState

        ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")
        finding_id = ks.add_finding(title=title, evidence=evidence)
        # Set biological_interpretation directly on the dict after creation
        for f in ks.data["findings"]:
            if f["id"] == finding_id:
                f["biological_interpretation"] = interpretation
                break
        ks.log_analysis(
            action="update_knowledge_state",
            finding_id=finding_id,
            title=title,
            description=description,
        )
        ks.save(ctx.job_dir / "knowledge_state.json")
        finding_count = len(ks.data["findings"])
        return f"✅ Finding #{finding_count} recorded: {title}"

    @tool
    def add_hypothesis(statement: str) -> str:
        """
        Add a new hypothesis to test.

        Args:
            statement: The hypothesis statement (e.g., 'X increases Y under Z conditions')

        Returns:
            Confirmation with hypothesis ID
        """
        from shandy.knowledge_state import KnowledgeState

        ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")
        hyp_id = ks.add_hypothesis(statement=statement, proposed_by="agent")
        ks.log_analysis(action="add_hypothesis", hypothesis_id=hyp_id, statement=statement)
        ks.save(ctx.job_dir / "knowledge_state.json")
        return f"✅ Hypothesis {hyp_id} added: {statement}"

    @tool
    def update_hypothesis(
        hypothesis_id: str,
        status: str,
        result_summary: str = "",
        p_value: str = "",
        effect_size: str = "",
        conclusion: str = "",
    ) -> str:
        """
        Update a hypothesis with test results.

        Args:
            hypothesis_id: Hypothesis ID (e.g., 'H001')
            status: New status - must be one of:
                    - "testing" - currently being tested
                    - "supported" - evidence supports the hypothesis
                    - "rejected" - evidence contradicts the hypothesis
            result_summary: Brief summary of test results
            p_value: P-value from statistical test (as string, e.g., "p=0.003")
            effect_size: Effect size (e.g., "Cohen's d=0.8", "r=0.45")
            conclusion: What this means for the research question

        Returns:
            Confirmation of update
        """
        from shandy.knowledge_state import KnowledgeState

        valid_statuses = ["pending", "testing", "supported", "rejected"]
        if status not in valid_statuses:
            return f"❌ Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}"

        ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")

        updates: dict[str, object] = {"status": status}
        if status in ("supported", "rejected"):
            updates["tested_at_iteration"] = ks.data["iteration"]
            updates["result"] = {
                "summary": result_summary,
                "p_value": p_value,
                "effect_size": effect_size,
                "conclusion": conclusion,
            }

        try:
            ks.update_hypothesis(hypothesis_id=hypothesis_id, updates=updates)
        except ValueError as e:
            return f"❌ {e}"

        ks.log_analysis(
            action="update_hypothesis",
            hypothesis_id=hypothesis_id,
            status=status,
            result_summary=result_summary,
        )
        ks.save(ctx.job_dir / "knowledge_state.json")

        status_emoji = {"testing": "🔬", "supported": "✅", "rejected": "❌"}.get(status, "📝")
        return f"{status_emoji} Hypothesis {hypothesis_id} updated to '{status}'"

    if use_hypotheses:
        return [update_knowledge_state, add_hypothesis, update_hypothesis]
    return [update_knowledge_state]

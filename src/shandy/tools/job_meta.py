"""
Job metadata tools for the SDK agent path.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from shandy.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)


def make_tools(ctx: ToolContext) -> list[Callable]:
    """Return job metadata tools (set_status, set_job_title, save_iteration_summary)."""

    @tool
    def set_status(message: str) -> str:
        """
        Update the agent's current status message (shown in the UI).

        Args:
            message: Status message (max 80 characters, e.g., 'Running PCA on expression data')

        Returns:
            Confirmation
        """
        from shandy.knowledge_state import KnowledgeState

        ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")
        ks.set_agent_status(message[:80])
        ks.save(ctx.job_dir / "knowledge_state.json")
        return f"✅ Status updated: {message[:80]}"

    @tool
    def set_job_title(title: str) -> str:
        """
        Set a brief, descriptive title for this job.

        Args:
            title: Short title (3-100 characters)

        Returns:
            Confirmation
        """
        import asyncio

        if len(title) > 100:
            return f"❌ Title too long ({len(title)} chars). Please keep it under 100 characters."

        if len(title) < 3:
            return "❌ Title too short. Please provide a meaningful title."

        config_path = ctx.job_dir / "config.json"
        if not config_path.exists():
            return "❌ config.json not found in job directory."

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        config["short_title"] = title
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        # Persist to database asynchronously
        job_id = config.get("job_id", ctx.job_dir.name)
        try:
            from uuid import UUID

            from sqlalchemy import update as sa_update

            from shandy.database.models.job import Job as JobModel
            from shandy.database.session import AsyncSessionLocal

            async def _update_db() -> None:
                async with AsyncSessionLocal(thread_safe=True) as session:
                    await session.execute(
                        sa_update(JobModel)
                        .where(JobModel.id == UUID(job_id))
                        .values(short_title=title)
                    )
                    await session.commit()

            try:
                asyncio.run(_update_db())
            except RuntimeError:
                loop = asyncio.get_event_loop()
                loop.create_task(_update_db())
        except Exception as e:
            logger.warning("Failed to persist job title to database: %s", e)

        return f"✅ Job title set: {title}"

    @tool
    def save_iteration_summary(summary: str, strapline: str = "") -> str:
        """
        Save a summary of this iteration's investigation and findings.

        Call this at the end of each iteration.

        Args:
            summary: 1-2 sentence summary of what you investigated and learned
            strapline: Optional one-line headline for this iteration

        Returns:
            Confirmation
        """
        from shandy.knowledge_state import KnowledgeState

        ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")
        ks.add_iteration_summary(
            iteration=ks.data["iteration"], summary=summary, strapline=strapline
        )
        ks.save(ctx.job_dir / "knowledge_state.json")
        return f"✅ Iteration summary saved: {summary[:100]}"

    @tool
    def set_consensus_answer(answer: str) -> str:
        """
        Set the consensus answer to the research question (1-3 sentences, direct).

        Call this after writing the final report.

        Args:
            answer: A direct 1-3 sentence answer to the research question

        Returns:
            Confirmation
        """
        from shandy.knowledge_state import KnowledgeState

        ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")
        ks.data["consensus_answer"] = answer.strip()
        ks.save(ctx.job_dir / "knowledge_state.json")
        return "✅ Consensus answer set"

    return [set_status, set_job_title, save_iteration_summary, set_consensus_answer]

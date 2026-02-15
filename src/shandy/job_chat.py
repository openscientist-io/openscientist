"""
Job chat service for interactive Q&A about SHANDY jobs.

Allows users to ask questions about their job results, findings, and
analysis process. The LLM is provided with comprehensive job context
including knowledge state, findings, hypotheses, and literature.
"""

import json
import logging
from pathlib import Path
from typing import List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import JobChatMessage

logger = logging.getLogger(__name__)


async def load_job_context(job_id: str, job_dir: Path) -> str:
    """
    Load comprehensive job context for LLM chat.

    Includes research question, findings, hypotheses, literature, and
    current analysis state.

    Args:
        job_id: Job ID
        job_dir: Path to job directory

    Returns:
        Formatted context string for LLM
    """
    context_parts = []

    # Load config
    config_path = job_dir / "config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
                research_question = config.get("research_question", "Unknown")
                context_parts.append(f"# Research Question\n{research_question}\n")
        except Exception as e:
            logger.warning("Failed to load config for job %s: %s", job_id, e)

    # Load knowledge state
    ks_path = job_dir / "knowledge_state.json"
    if ks_path.exists():
        try:
            with open(ks_path) as f:
                ks = json.load(f)

                # Findings
                findings = ks.get("findings", [])
                if findings:
                    context_parts.append("# Findings")
                    for i, finding in enumerate(findings, 1):
                        importance = finding.get("importance", "unknown")
                        confidence = finding.get("confidence", "unknown")
                        context_parts.append(
                            f"\n## Finding {i} (Importance: {importance}, Confidence: {confidence})"
                        )
                        context_parts.append(finding.get("content", ""))

                        # Evidence
                        evidence = finding.get("evidence", [])
                        if evidence:
                            context_parts.append("\nEvidence:")
                            for ev in evidence:
                                context_parts.append(f"- {ev}")
                    context_parts.append("")

                # Hypotheses
                hypotheses = ks.get("hypotheses", [])
                if hypotheses:
                    context_parts.append("# Hypotheses")
                    for i, hyp in enumerate(hypotheses, 1):
                        status = hyp.get("status", "unknown")
                        context_parts.append(f"\n## Hypothesis {i} (Status: {status})")
                        context_parts.append(hyp.get("hypothesis", ""))

                        # Rationale
                        rationale = hyp.get("rationale")
                        if rationale:
                            context_parts.append(f"\nRationale: {rationale}")
                    context_parts.append("")

                # Literature
                literature = ks.get("literature", [])
                if literature:
                    context_parts.append("# Literature Reviewed")
                    for i, lit in enumerate(literature, 1):
                        title = lit.get("title", "Unknown")
                        relevance = lit.get("relevance_score", "unknown")
                        context_parts.append(f"\n## Paper {i} (Relevance: {relevance})")
                        context_parts.append(f"Title: {title}")

                        # Key findings from paper
                        key_findings = lit.get("key_findings", [])
                        if key_findings:
                            context_parts.append("Key findings:")
                            for kf in key_findings:
                                context_parts.append(f"- {kf}")
                    context_parts.append("")

                # Iteration summaries
                summaries = ks.get("iteration_summaries", [])
                if summaries:
                    context_parts.append("# Analysis Progress")
                    for summary in summaries:
                        iteration = summary.get("iteration", 0)
                        strapline = summary.get("strapline", "")
                        summary_text = summary.get("summary", "")
                        context_parts.append(f"\n## Iteration {iteration}: {strapline}")
                        context_parts.append(summary_text)
                    context_parts.append("")

        except Exception as e:
            logger.warning("Failed to load knowledge state for job %s: %s", job_id, e)

    return "\n".join(context_parts)


async def get_chat_history(
    session: AsyncSession,
    job_id: UUID,
    limit: int = 50,
) -> List[JobChatMessage]:
    """
    Get chat message history for a job.

    Args:
        session: Database session
        job_id: Job ID
        limit: Maximum number of messages to return

    Returns:
        List of chat messages, ordered chronologically
    """
    stmt = (
        select(JobChatMessage)
        .where(JobChatMessage.job_id == job_id)
        .order_by(JobChatMessage.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def send_chat_message(
    session: AsyncSession,
    job_id: UUID,
    message: str,
    job_dir: Path,
) -> str:
    """
    Send a chat message and get LLM response.

    Args:
        session: Database session
        job_id: Job ID
        message: User's message
        job_dir: Path to job directory

    Returns:
        LLM's response text

    Raises:
        Exception: If API call fails
    """
    # Load job context
    context = await load_job_context(str(job_id), job_dir)

    # Use SDK directly
    assistant_message = await _send_message_via_sdk(session, job_id, message, context)

    # Store both messages in database
    user_msg = JobChatMessage(
        job_id=job_id,
        role="user",
        content=message,
    )
    session.add(user_msg)

    assistant_msg = JobChatMessage(
        job_id=job_id,
        role="assistant",
        content=assistant_message,
    )
    session.add(assistant_msg)

    await session.commit()

    return assistant_message


async def _send_message_via_sdk(
    session: AsyncSession,
    job_id: UUID,
    message: str,
    context: str,
) -> str:
    """
    Send message using Anthropic SDK directly.

    This bypasses the Claude Code CLI and its local pre-flight content filter,
    which can produce false positives on legitimate scientific content.
    """
    from shandy.providers import get_provider

    # Get chat history for continuity
    history = await get_chat_history(session, job_id, limit=10)

    # Build system prompt
    system_prompt = f"""You are a research assistant helping a scientist analyze the results of their SHANDY literature review and hypothesis generation job.

SHANDY is a scientific research tool that autonomously reviews published academic literature, generates hypotheses, and synthesizes findings. This is an academic research context where users are discussing published scientific papers and research methodology.

Your role is to:
1. Discuss the findings from the literature review and their academic significance
2. Explain the research methodology and analysis process
3. Clarify scientific concepts mentioned in the reviewed papers
4. Help interpret the synthesized results in the context of the research question

Important: You are discussing published research and scientific literature. You are not providing personal advice - you are helping analyze what the scientific literature says.

Here is the complete job context with findings from published academic papers:

{context}

Be concise, accurate, and cite specific papers or findings when relevant. Focus on what the research literature indicates."""

    # Build messages from chat history
    messages: list[dict[str, str]] = []
    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})

    # Add the current user message
    messages.append({"role": "user", "content": message})

    logger.info(
        "Chat SDK call: %d messages, system prompt %d chars",
        len(messages),
        len(system_prompt),
    )

    # Call SDK via provider
    provider = get_provider()
    response = await provider.send_message(
        messages=messages,
        system=system_prompt,
    )

    return response


async def clear_chat_history(
    session: AsyncSession,
    job_id: UUID,
) -> int:
    """
    Clear chat history for a job.

    Args:
        session: Database session
        job_id: Job ID

    Returns:
        Number of messages deleted
    """
    stmt = select(JobChatMessage).where(JobChatMessage.job_id == job_id)
    result = await session.execute(stmt)
    messages = result.scalars().all()

    count = len(messages)
    for msg in messages:
        await session.delete(msg)

    await session.commit()

    return count

"""
Job chat service for interactive Q&A about OpenScientist jobs.

Allows users to ask questions about their job results, findings, and
analysis process. Uses ClaudeCodeAgent for responses, giving the agent
access to tools (execute_code, search_pubmed, etc.) for follow-up analysis.
"""

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, JobChatMessage
from openscientist.database.session import AsyncSessionLocal
from openscientist.knowledge_state import KnowledgeState

logger = logging.getLogger(__name__)


async def _load_research_question_from_db(job_id: str) -> str | None:
    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            result = await session.execute(
                select(Job.research_question).where(Job.id == UUID(job_id))
            )
            value = result.scalar_one_or_none()
        if isinstance(value, str) and value.strip():
            return value
    except ValueError:
        logger.debug("Skipping DB lookup for non-UUID job id: %s", job_id)
    except Exception as e:
        logger.warning("Failed to load DB context for job %s: %s", job_id, e)
    return None


def _append_research_question(parts: list[str], question: str) -> None:
    parts.append(f"# Research Question\n{question}\n")


def _extract_research_question_from_ks(ks: dict[str, Any]) -> str | None:
    ks_config = ks.get("config", {})
    if not isinstance(ks_config, dict):
        return None
    question = ks_config.get("research_question")
    if isinstance(question, str) and question.strip():
        return question.strip()
    return None


def _append_findings(parts: list[str], findings: list[Any]) -> None:
    if not findings:
        return
    parts.append("# Findings")
    for i, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            continue
        importance = finding.get("importance", "unknown")
        confidence = finding.get("confidence", "unknown")
        parts.append(f"\n## Finding {i} (Importance: {importance}, Confidence: {confidence})")
        finding_text = finding.get("content", finding.get("title", ""))
        parts.append(finding_text if isinstance(finding_text, str) else str(finding_text))

        evidence = finding.get("evidence", [])
        if evidence:
            parts.append("\nEvidence:")
            parts.extend(f"- {ev}" for ev in evidence)
    parts.append("")


def _append_hypotheses(parts: list[str], hypotheses: list[Any]) -> None:
    if not hypotheses:
        return
    parts.append("# Hypotheses")
    for i, hyp in enumerate(hypotheses, 1):
        if not isinstance(hyp, dict):
            continue
        status = hyp.get("status", "unknown")
        parts.append(f"\n## Hypothesis {i} (Status: {status})")
        hypothesis_text = hyp.get("hypothesis", hyp.get("statement", ""))
        parts.append(hypothesis_text if isinstance(hypothesis_text, str) else str(hypothesis_text))

        rationale = hyp.get("rationale")
        if rationale:
            parts.append(f"\nRationale: {rationale}")
    parts.append("")


def _append_literature(parts: list[str], literature: list[Any]) -> None:
    if not literature:
        return
    parts.append("# Literature Reviewed")
    for i, lit in enumerate(literature, 1):
        if not isinstance(lit, dict):
            continue
        title = lit.get("title", "Unknown")
        relevance = lit.get("relevance_score", "unknown")
        parts.append(f"\n## Paper {i} (Relevance: {relevance})")
        parts.append(f"Title: {title}")

        key_findings = lit.get("key_findings", [])
        if key_findings:
            parts.append("Key findings:")
            parts.extend(f"- {kf}" for kf in key_findings)
    parts.append("")


def _append_iteration_summaries(parts: list[str], summaries: list[Any]) -> None:
    if not summaries:
        return
    parts.append("# Analysis Progress")
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        iteration = summary.get("iteration", 0)
        strapline = summary.get("strapline", "")
        summary_text = summary.get("summary", "")
        parts.append(f"\n## Iteration {iteration}: {strapline}")
        parts.append(summary_text)
    parts.append("")


def _load_knowledge_state(job_id: str) -> dict[str, Any] | None:
    try:
        payload = KnowledgeState.load_from_database_sync(job_id).to_dict()
    except Exception as e:
        logger.warning("Failed to load knowledge state for job %s: %s", job_id, e)
        return None
    return payload if isinstance(payload, dict) else None


async def load_job_context(job_id: str) -> str:
    """
    Load comprehensive job context for LLM chat.

    Includes research question, findings, hypotheses, literature, and
    current analysis state.

    Args:
        job_id: Job ID

    Returns:
        Formatted context string for LLM
    """
    context_parts: list[str] = []
    research_question = await _load_research_question_from_db(job_id)
    if research_question:
        _append_research_question(context_parts, research_question)

    ks = _load_knowledge_state(job_id)
    if not ks:
        return "\n".join(context_parts)

    if not context_parts:
        fallback_question = _extract_research_question_from_ks(ks)
        if fallback_question:
            _append_research_question(context_parts, fallback_question)

    _append_findings(context_parts, ks.get("findings", []))
    _append_hypotheses(context_parts, ks.get("hypotheses", []))
    _append_literature(context_parts, ks.get("literature", []))
    _append_iteration_summaries(context_parts, ks.get("iteration_summaries", []))
    return "\n".join(context_parts)


async def get_chat_history(
    session: AsyncSession,
    job_id: UUID,
    limit: int = 50,
) -> list[JobChatMessage]:
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
    Send a chat message and get LLM response via ClaudeCodeAgent.

    Args:
        session: Database session
        job_id: Job ID
        message: User's message
        job_dir: Path to job directory

    Returns:
        LLM's response text

    Raises:
        Exception: If executor call fails
    """
    # Use executor (context is read on-demand by the agent from job_dir files)
    assistant_message = await _send_message_via_executor(session, job_id, message, job_dir)

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


# Prior assistant turns can be full report dumps. Replaying them verbatim
# few-shots the model into dumping again, so cap each history message. Recent
# intent matters more than verbatim length, and the agent can re-read job files.
_HISTORY_MAX_CHARS = 800


def _truncate_history(content: str) -> str:
    """Cap a prior chat message so a long assistant report dump does not prime
    the model to repeat it."""
    content = content.strip()
    if len(content) <= _HISTORY_MAX_CHARS:
        return content
    return content[:_HISTORY_MAX_CHARS].rstrip() + " [...truncated]"


async def _send_message_via_executor(
    session: AsyncSession,
    job_id: UUID,
    message: str,
    job_dir: Path,
) -> str:
    """
    Send a chat message through the configured agent backend.

    Creates a short-lived executor via the agent factory (ClaudeCodeAgent for
    Claude-compatible providers, CodexAgent for codex-compatible ones such as
    Ollama) with the chat system prompt and full tool access, allowing the
    agent to re-analyze data or search literature when answering follow-ups.
    """
    from openscientist.agent.base import AgentConfig
    from openscientist.agent.factory import agent_class_for_provider, build_agent
    from openscientist.exec_broker_client import (
        EXEC_BROKER_URL_ENV,
        EXEC_TOKEN_ENV,
        container_broker_base_url,
    )
    from openscientist.job_container.secrets import make_exec_placeholder
    from openscientist.providers import get_provider
    from openscientist.settings import get_settings

    # Get chat history for continuity
    history = await get_chat_history(session, job_id, limit=10)

    # System prompt is kept small (it's passed as a CLI arg to the claude
    # subprocess, so large payloads hit the OS ARG_MAX limit).  The agent
    # can read job data files on demand via Claude Code's built-in Read tool.
    system_prompt = """You are a research assistant helping a scientist discuss the results of their OpenScientist literature review and hypothesis generation job.

Your working directory is the job folder. Use available tools to inspect artifacts
and data when you need details.

Your role is to:
1. Discuss the findings from the literature review and their academic significance
2. Explain the research methodology and analysis process
3. Clarify scientific concepts mentioned in the reviewed papers
4. Help interpret the synthesized results in the context of the research question

Important: You are discussing published research and scientific literature. You are not providing personal advice — you are helping analyze what the scientific literature says.

Be concise, accurate, and cite specific papers or findings when relevant. Focus on what the research literature indicates."""

    # Prompt structure matters more than wording: findings are framed as
    # background reference, long prior turns are truncated so an old report dump
    # does not few-shot a repeat, and the live user message comes LAST.
    prompt_parts = []

    job_context = await load_job_context(str(job_id))
    if job_context.strip():
        prompt_parts.append(
            "## Background reference for this job\n"
            "Context from the completed job, for you to draw on when answering. "
            "Do NOT recite or summarize it unless the user asks. Read "
            "`final_report.md` if you need full detail."
        )
        prompt_parts.append(job_context)
        prompt_parts.append("---")

    if history:
        prompt_parts.append("## Conversation so far")
        for msg in history:
            role_label = "User" if msg.role == "user" else "Assistant"
            prompt_parts.append(f"{role_label}: {_truncate_history(msg.content)}")
        prompt_parts.append("---")

    prompt_parts.append("## The user's new message (answer THIS, nothing else)")
    prompt_parts.append(
        "Respond directly to the message below. If it is not a clear question or "
        "request, reply briefly and ask what they would like to know. Do not "
        "summarize the job's findings unless the user explicitly asks for a "
        "summary."
    )
    prompt_parts.append(f"User: {message}")

    prompt = "\n".join(prompt_parts)

    logger.info(
        "Chat executor call: %d history messages, system prompt %d chars",
        len(history),
        len(system_prompt),
    )

    # Backend-specific chat prep flows through the AbstractAgent contract, not
    # isinstance: the agent class (from the provider) supplies the system prompt
    # and model override, then the executor applies its own auth and context
    # setup (no-ops on codex).
    provider = get_provider()
    agent_cls = agent_class_for_provider(provider)
    config = AgentConfig(
        job_dir=job_dir,
        system_prompt=agent_cls.chat_system_prompt(system_prompt),
        model_override=agent_cls.chat_model_override(),
        tool_server_env={
            # Chat's tools run in-process here, so hand them this job's exec token.
            EXEC_TOKEN_ENV: make_exec_placeholder(get_settings().secret_key, str(job_id)),
            EXEC_BROKER_URL_ENV: container_broker_base_url(),
        },
    )
    executor = build_agent(config, provider)
    executor.apply_runtime_environment()
    executor.write_chat_context()

    try:
        result = await executor.run_iteration(prompt, reset_session=True)
        if not result.success:
            raise RuntimeError(result.error or "Chat executor returned no output")
        return result.output
    finally:
        await executor.shutdown()

"""
Persistent, user-scoped memory for MARDUK rare-disease jobs.

Read/write helpers over the ``marduk_memories`` table. A MARDUK job records
durable insights here; later jobs by the same user recall them (both via the
``recall_memory`` tool and via injection into the job workspace at start-up).

Design notes:
- Every query filters explicitly by ``owner_id``. The agent container connects
  to Postgres with a privileged role that bypasses RLS, so ownership scoping is
  enforced here in application code, not by the DB policy (the RLS policy still
  protects the webapp path where users read their own memories).
- Recall uses simple case-insensitive substring matching over title/content/
  entity fields — memory volume per user is small, so this is sufficient and
  avoids the tsvector/trigger machinery skills use.
- The async functions use the thread-safe session factory (NullPool) so they are
  safe to call from the tools subprocess and orchestrator worker threads, exactly
  like ``KnowledgeState``.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select

from openscientist.async_tasks import run_sync
from openscientist.database.models import Job, MardukMemory
from openscientist.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# CURIEs like MONDO:0007947, HP:0001250, HGNC:3603, OMIM:154700, ORPHA:558.
_CURIE_RE = re.compile(r"\b(?:MONDO|HP|HGNC|OMIM|ORPHA|NCBIGene|MGI|ZFIN|UBERON|DOID):[0-9A-Za-z]+")

# KnowledgeState is imported lazily inside extract_memories_from_job to avoid a
# heavy import at module load.


async def _lookup_owner_id(session: Any, job_id: str) -> UUID | None:
    """Return the owner_id for a job, or None if the job is missing."""
    result = await session.execute(select(Job.owner_id).where(Job.id == UUID(job_id)))
    owner_id: UUID | None = result.scalar_one_or_none()
    return owner_id


async def save_memory(
    *,
    job_id: str,
    title: str,
    content: str,
    kind: str = "insight",
    entity_id: str | None = None,
    entity_label: str | None = None,
    evidence: str | None = None,
    tags: list[str] | None = None,
) -> UUID | None:
    """Persist one memory owned by the job's owner.

    Returns the new memory id, or None if the job (hence owner) is not found.
    """
    async with AsyncSessionLocal(thread_safe=True) as session:
        owner_id = await _lookup_owner_id(session, job_id)
        if owner_id is None:
            logger.warning("save_memory: job %s not found; skipping", job_id)
            return None
        memory = MardukMemory(
            owner_id=owner_id,
            source_job_id=UUID(job_id),
            kind=kind,
            entity_id=entity_id,
            entity_label=entity_label,
            title=title[:500],
            content=content,
            evidence=evidence,
            tags=tags or [],
        )
        session.add(memory)
        await session.commit()
        await session.refresh(memory)
        return memory.id


def save_memory_sync(**kwargs: Any) -> UUID | None:
    """Synchronous wrapper for :func:`save_memory`."""
    return run_sync(save_memory(**kwargs))


async def recall_memories(
    *,
    owner_id: UUID,
    query: str | None = None,
    entity_id: str | None = None,
    exclude_job_id: str | None = None,
    limit: int = 10,
) -> list[MardukMemory]:
    """Return this owner's active memories, optionally filtered by text/entity.

    Ordered newest-first. ``exclude_job_id`` drops memories produced by the
    current job so a running job does not recall what it just wrote.
    """
    async with AsyncSessionLocal(thread_safe=True) as session:
        stmt = select(MardukMemory).where(
            MardukMemory.owner_id == owner_id,
            MardukMemory.is_active.is_(True),
        )
        if exclude_job_id is not None:
            stmt = stmt.where(
                or_(
                    MardukMemory.source_job_id.is_(None),
                    MardukMemory.source_job_id != UUID(exclude_job_id),
                )
            )
        if entity_id:
            stmt = stmt.where(MardukMemory.entity_id == entity_id)
        if query:
            like = f"%{query}%"
            stmt = stmt.where(
                or_(
                    MardukMemory.title.ilike(like),
                    MardukMemory.content.ilike(like),
                    MardukMemory.entity_label.ilike(like),
                    MardukMemory.entity_id.ilike(like),
                )
            )
        stmt = stmt.order_by(MardukMemory.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


def recall_memories_sync(**kwargs: Any) -> list[MardukMemory]:
    """Synchronous wrapper for :func:`recall_memories`."""
    return run_sync(recall_memories(**kwargs))


def extract_curies(*texts: str | None) -> list[str]:
    """Return unique CURIEs found across the given texts, in first-seen order."""
    seen: dict[str, None] = {}
    for text_val in texts:
        if not text_val:
            continue
        for match in _CURIE_RE.findall(text_val):
            seen.setdefault(match, None)
    return list(seen.keys())


async def extract_memories_from_job(job_id: str) -> int:
    """Auto-sweep: distill one durable memory from a completed job's knowledge.

    Heuristic (no extra LLM call): summarize the job's consensus answer and top
    findings into a single memory, tagging any Monarch CURIEs mentioned. Returns
    the number of memories written (0 or 1). Safe to call unconditionally at job
    end — it no-ops on empty jobs and never raises for missing data.
    """
    from openscientist.knowledge_state import KnowledgeState

    try:
        ks = KnowledgeState.load_from_database_sync(job_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("extract_memories_from_job: could not load KS for %s: %s", job_id, exc)
        return 0

    research_question = ks.data.get("config", {}).get("research_question") or ""
    consensus = (ks.data.get("consensus_answer") or "").strip()
    findings = ks.data.get("findings") or []

    if not consensus and not findings:
        logger.info("extract_memories_from_job: nothing to record for %s", job_id)
        return 0

    finding_texts = [str(f.get("text", "")) for f in findings if f.get("text")]
    if consensus:
        content = consensus
    else:
        top = finding_texts[:5]
        content = "Key findings:\n" + "\n".join(f"- {t}" for t in top)

    curies = extract_curies(consensus, research_question, *finding_texts)
    title = (research_question or "MARDUK investigation").strip()

    evidence_bits = [f"Auto-recorded from job {job_id}"]
    if findings:
        evidence_bits.append(f"{len(findings)} findings")
    iteration = ks.data.get("iteration")
    if iteration:
        evidence_bits.append(f"{iteration} iterations")
    evidence = "; ".join(evidence_bits)

    memory_id = await save_memory(
        job_id=job_id,
        title=title,
        content=content,
        kind="insight",
        entity_id=curies[0] if curies else None,
        evidence=evidence,
        tags=curies,
    )
    return 1 if memory_id is not None else 0


def extract_memories_from_job_sync(job_id: str) -> int:
    """Synchronous wrapper for :func:`extract_memories_from_job`."""
    return run_sync(extract_memories_from_job(job_id))


def format_memories_markdown(memories: list[MardukMemory]) -> str:
    """Render memories as a markdown briefing for injection / recall output."""
    if not memories:
        return "No prior MARDUK memories for this user."
    parts = [f"{len(memories)} prior MARDUK memory item(s):\n"]
    for i, m in enumerate(memories, 1):
        header = f"\n{i}. **{m.title}**"
        if m.entity_id:
            label = f" ({m.entity_label})" if m.entity_label else ""
            header += f" — `{m.entity_id}`{label}"
        parts.append(header + "\n")
        parts.append(f"   {m.content}\n")
        if m.evidence:
            parts.append(f"   _Evidence: {m.evidence}_\n")
        if m.tags:
            parts.append(f"   _Tags: {', '.join(str(t) for t in m.tags)}_\n")
    return "".join(parts)


async def write_memory_briefing(job_dir: Any, *, exclude_job_id: str) -> None:
    """Write this user's prior memories into ``job_dir/.claude/MARDUK_MEMORY.md``.

    Called at job start for MARDUK jobs so the agent sees relevant history. The
    owner is resolved from ``exclude_job_id`` (the current job). No-op (and never
    raises) when there is nothing to inject.
    """
    from pathlib import Path

    job_dir = Path(job_dir)
    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            owner_id = await _lookup_owner_id(session, exclude_job_id)
        if owner_id is None:
            return
        memories = await recall_memories(owner_id=owner_id, exclude_job_id=exclude_job_id, limit=25)
        if not memories:
            return
        claude_dir = job_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        briefing = (
            "# MARDUK memory\n\n"
            "Durable insights recorded by your previous rare-disease jobs. Read these "
            "early — a prior job may already have resolved an entity, established a "
            "gene-disease link, or ruled out a candidate. Use `recall_memory` to search "
            "for more.\n\n"
        )
        (claude_dir / "MARDUK_MEMORY.md").write_text(
            briefing + format_memories_markdown(memories), encoding="utf-8"
        )
        logger.info(
            "Wrote %d MARDUK memories to workspace for job %s", len(memories), exclude_job_id
        )
    except Exception as exc:  # pragma: no cover - defensive, must not block job start
        logger.warning("Failed to write MARDUK memory briefing: %s", exc)

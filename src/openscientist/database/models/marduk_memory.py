"""
MardukMemory model for persistent, cross-job rare-disease knowledge.

A MARDUK job can record durable insights (confirmed disease-gene links, useful
CURIE mappings, ruled-out hypotheses) that later jobs by the *same user* can
recall. Unlike per-job ``KnowledgeState`` rows, a memory is scoped to its owner
(not to a job) so it outlives the job that produced it — the ``source_job_id``
FK uses ``ON DELETE SET NULL`` so deleting the originating job keeps the memory.

This is the persistence half of MARDUK; the read half is injection into new job
workspaces at start-up plus the ``recall_memory`` tool.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job
    from .user import User


class MardukMemory(UUIDv7Mixin, Base):
    """
    A durable, user-scoped rare-disease insight recorded by a MARDUK job.

    Attributes:
        owner_id: User who owns this memory (results are private to them).
        source_job_id: Job that produced the memory (NULL if that job was
            deleted).
        kind: Coarse memory type (insight/mapping/ruled_out/association).
        entity_id: Primary Monarch CURIE the memory is about (e.g. MONDO:...),
            used for recall by entity.
        entity_label: Human-readable label for ``entity_id``.
        title: Short headline for the memory.
        content: The insight itself.
        evidence: Supporting evidence / provenance for the insight.
        tags: Array of free-text tags (diseases, genes, phenotypes) for recall.
        is_active: Whether the memory is eligible for recall/injection.
    """

    __tablename__ = "marduk_memories"

    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who owns this memory",
    )

    source_job_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Job that produced this memory (NULL if that job was deleted)",
    )

    kind: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="insight",
        comment="Memory type (insight/mapping/ruled_out/association)",
    )

    entity_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="Primary Monarch CURIE the memory is about (e.g. MONDO:0007947)",
    )

    entity_label: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Human-readable label for entity_id",
    )

    title: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Short headline for the memory",
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The insight itself",
    )

    evidence: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Supporting evidence / provenance for the insight",
    )

    tags: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
        comment="Free-text tags (diseases, genes, phenotypes) for recall",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        index=True,
        comment="Whether the memory is eligible for recall/injection",
    )

    # Relationships
    owner: Mapped["User"] = relationship()
    source_job: Mapped["Job | None"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<MardukMemory(id={self.id}, owner_id={self.owner_id}, "
            f"entity_id={self.entity_id}, title={self.title[:40]!r})>"
        )

"""
Literature model for scientific references.

Tracks literature references (papers, documentation) retrieved
during analysis to support hypotheses and findings.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .finding import Finding
    from .job import Job


class Literature(UUIDv7Mixin, Base):
    """
    Literature reference.

    Represents a scientific paper, documentation, or other reference
    retrieved during analysis. Literature is linked to hypotheses and
    findings it supports.

    Attributes:
        job_id: Foreign key to the job this reference belongs to
        iteration: Iteration number when reference was retrieved
        title: Title of the paper/document
        authors: Author list
        year: Publication year
        url: URL to the resource
        abstract: Abstract or summary
        extra_metadata: Additional structured metadata
        job: Related Job object
        hypotheses: Hypotheses this reference supports (M2M)
        findings: Findings this reference supports (M2M)
    """

    __tablename__ = "literature"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this reference belongs to",
    )

    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Iteration number when reference was retrieved",
    )

    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Title of the paper/document",
    )

    authors: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Author list",
    )

    year: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Publication year",
    )

    url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="URL to the resource",
    )

    abstract: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Abstract or summary",
    )

    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Additional structured metadata",
    )

    # Relationships
    job: Mapped["Job"] = relationship()

    # Note: Hypotheses were incorrectly linked via finding_literature.
    # If direct hypothesis-literature relationships are needed in the future,
    # create a hypothesis_literature junction table.

    findings: Mapped[list["Finding"]] = relationship(
        secondary="finding_literature",
        back_populates="literature",
    )

    def __repr__(self) -> str:
        return f"<Literature(id={self.id}, title={self.title[:50]}..., year={self.year})>"

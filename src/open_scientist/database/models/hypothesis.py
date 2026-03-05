"""
Hypothesis model for analysis hypotheses.

Hypotheses are central to the OpenScientist workflow, representing testable
explanations for crystallographic observations that drive analysis.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .finding import Finding
    from .hypothesis_spawn import HypothesisSpawn
    from .job import Job


class Hypothesis(UUIDv7Mixin, Base):
    """
    Analysis hypothesis.

    Represents a testable explanation for crystallographic observations.
    Hypotheses are generated, tested, refined, and spawn new hypotheses
    through the iterative analysis workflow.

    Attributes:
        job_id: Foreign key to the job this hypothesis belongs to
        iteration: Iteration number when hypothesis was generated
        text: Hypothesis statement
        status: Current status (active/tested/disproved/merged/spawned)
        confidence: Confidence score (0.0-1.0)
        priority: Priority for testing (higher = more important)
        rationale: Reasoning behind the hypothesis
        test_strategy: Proposed method for testing
        supporting_evidence: Evidence supporting this hypothesis
        job: Related Job object
        findings: Findings that support this hypothesis (M2M)
        literature: Literature references (M2M)
        parent_spawns: When this hypothesis spawned from a parent
        child_spawns: Hypotheses spawned from this one
    """

    __tablename__ = "hypotheses"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this hypothesis belongs to",
    )

    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Iteration number when hypothesis was generated",
    )

    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Hypothesis statement",
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="active",
        comment="Current status (active/tested/disproved/merged/spawned)",
    )

    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Confidence score (0.0-1.0)",
    )

    priority: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Priority for testing (higher = more important)",
    )

    rationale: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Reasoning behind the hypothesis",
    )

    test_strategy: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Proposed method for testing",
    )

    supporting_evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Evidence supporting this hypothesis",
    )

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="hypotheses")

    findings: Mapped[list["Finding"]] = relationship(
        secondary="finding_hypotheses",
        back_populates="hypotheses",
    )

    # Note: Hypotheses access literature indirectly through their findings
    # If direct hypothesis-literature relationships are needed in the future,
    # create a hypothesis_literature junction table

    parent_spawns: Mapped[list["HypothesisSpawn"]] = relationship(
        foreign_keys="HypothesisSpawn.child_id",
        back_populates="child",
    )

    child_spawns: Mapped[list["HypothesisSpawn"]] = relationship(
        foreign_keys="HypothesisSpawn.parent_id",
        back_populates="parent",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Hypothesis(id={self.id}, iteration={self.iteration}, "
            f"status={self.status}, text={self.text[:50]}...)>"
        )

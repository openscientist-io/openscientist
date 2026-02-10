"""
Hypothesis spawn relationship model.

Tracks parent-child relationships when hypotheses spawn new hypotheses
based on findings or analysis results.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .hypothesis import Hypothesis


class HypothesisSpawn(UUIDv7Mixin, Base):
    """
    Hypothesis spawn relationship.

    Tracks when a hypothesis spawns a new hypothesis based on findings
    or refinement. This creates a genealogy of hypotheses.

    Attributes:
        parent_id: Foreign key to the parent hypothesis
        child_id: Foreign key to the spawned child hypothesis
        spawn_reason: Explanation for why child was spawned
        parent: Related parent Hypothesis object
        child: Related child Hypothesis object
    """

    __tablename__ = "hypothesis_spawns"

    parent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Parent hypothesis that spawned the child",
    )

    child_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Child hypothesis that was spawned",
    )

    spawn_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Explanation for why child was spawned",
    )

    # Relationships
    parent: Mapped["Hypothesis"] = relationship(
        foreign_keys=[parent_id],
        back_populates="child_spawns",
    )

    child: Mapped["Hypothesis"] = relationship(
        foreign_keys=[child_id],
        back_populates="parent_spawns",
    )

    def __repr__(self) -> str:
        return (
            f"<HypothesisSpawn(id={self.id}, parent_id={self.parent_id}, child_id={self.child_id})>"
        )

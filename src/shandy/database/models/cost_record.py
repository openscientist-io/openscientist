"""
Cost record model for LLM usage tracking.

Tracks LLM API costs per job and per operation for billing transparency
and budget enforcement.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class CostRecord(UUIDv7Mixin, Base):
    """
    LLM cost tracking record.

    Records costs for each LLM API call during job execution.
    Used for billing transparency and budget enforcement.

    Attributes:
        job_id: Foreign key to the job this cost belongs to
        iteration: Iteration number when cost was incurred (NULL for chat)
        operation_type: Type of operation (analysis/code_gen/chat/etc)
        provider: LLM provider (vertex/bedrock/cborg)
        model: Model name/ID used
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        cost_usd: Cost in USD
        job: Related Job object
    """

    __tablename__ = "cost_records"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this cost belongs to",
    )

    iteration: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Iteration number when cost was incurred (NULL for chat)",
    )

    operation_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Type of operation (analysis/code_gen/chat/etc)",
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="LLM provider (vertex/bedrock/cborg)",
    )

    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Model name/ID used",
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Number of input tokens",
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Number of output tokens",
    )

    cost_usd: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Cost in USD",
    )

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="cost_records")

    def __repr__(self) -> str:
        return (
            f"<CostRecord(id={self.id}, job_id={self.job_id}, "
            f"operation={self.operation_type}, cost=${self.cost_usd:.4f})>"
        )

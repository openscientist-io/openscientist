"""How long the upstream took to answer one Messages call through the LLM proxy."""

from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, UUIDv7Mixin


class LlmCallTiming(UUIDv7Mixin, Base):
    """The upstream timing of one Messages call. ``created_at`` is when the
    proxy sent it, and every duration counts from then."""

    __tablename__ = "llm_call_timings"
    __table_args__ = (
        CheckConstraint(
            "ended IN ('complete', 'client_disconnected', 'upstream_ended_early')",
            name="ck_llm_call_timings_ended",
        ),
    )

    job_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Job whose run made the call",
    )
    cost_record_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("cost_records.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        comment="Cost of the call, absent when the provider does not bill it",
    )
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    streamed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Upstream status, absent when no response arrived"
    )
    headers_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_event_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    longest_event_gap_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="From the final usage, absent when none arrived"
    )
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ended: Mapped[str] = mapped_column(String(32), nullable=False)

"""Expert model — subagent definitions loaded into the SDK ``agents`` dict."""

from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, UUIDv7Mixin


class Expert(UUIDv7Mixin, Base):
    """A subagent definition consumed by the SDK ``agents=`` kwarg."""

    __tablename__ = "experts"

    slug: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Unique URL-friendly identifier used as the subagent key",
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable expert name",
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Short description that drives SDK auto-delegation",
    )

    prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="System prompt body passed to the subagent",
    )

    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Category tag (research | domain | methodology | code)",
    )

    source: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Origin of the prompt (openscientist | anthropic | wshobson | voltagent)",
    )

    tools: Mapped[list[str] | None] = mapped_column(
        # none_as_null: Python None → SQL NULL (not JSONB null), required by CHECK constraint.
        JSONB(none_as_null=True),
        nullable=True,
        comment="Optional tool allowlist; NULL means inherit parent's tools",
    )

    model: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Optional model pin (sonnet | opus | haiku | inherit); NULL = inherit",
    )

    source_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Upstream file URL for attribution of vendored prompts",
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Whether this expert is loaded into the SDK agents= dict",
    )

    def __repr__(self) -> str:
        return f"<Expert(id={self.id}, slug={self.slug}, source={self.source})>"

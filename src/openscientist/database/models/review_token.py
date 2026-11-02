"""Review token model for anonymous reviewer access."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .user import User


class ReviewToken(UUIDv7Mixin, Base):
    """Magic-link token for anonymous reviewer access.

    The label becomes the reviewer's display name. The same token can be
    redeemed multiple times to re-login the same anonymous user.
    """

    __tablename__ = "review_tokens"

    token_hash: Mapped[str] = mapped_column(
        Text,
        unique=True,
        nullable=False,
        comment="SHA-256 hash of the plaintext token",
    )

    label: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Admin label, used as reviewer display name",
    )

    created_by_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Admin user who created this token",
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the token expires (null = no expiry)",
    )

    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the token was first redeemed",
    )

    redeemed_by_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Anonymous user created on first redemption",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Whether the token is active (admin can revoke)",
    )

    # Relationships
    created_by: Mapped["User"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )

    redeemed_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[redeemed_by_id],
    )

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    @property
    def is_redeemed(self) -> bool:
        return self.redeemed_at is not None

    @property
    def status(self) -> str:
        if not self.is_active:
            return "revoked"
        if self.is_expired:
            return "expired"
        if self.is_redeemed:
            return "redeemed"
        return "active"

    def __repr__(self) -> str:
        return f"<ReviewToken(id={self.id}, label={self.label!r}, status={self.status})>"

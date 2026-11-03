"""API key model for REST API authentication."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .user import User


class APIKey(UUIDv7Mixin, Base):
    """API key for REST API authentication.

    The key is "name:secret". The name is stored in plaintext, the secret is
    hashed, and the full key is shown only once at creation.
    """

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_api_keys_user_name"),)

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who owns this API key",
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="User-chosen descriptive name for the key",
    )

    key_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        comment="Hashed secret portion of the API key",
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when key was last used (UTC)",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Whether the key is active (for revocation)",
    )

    usage_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="Number of times this API key has been used",
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="api_keys")

    def __repr__(self) -> str:
        return f"<APIKey(id={self.id}, name={self.name}, is_active={self.is_active})>"

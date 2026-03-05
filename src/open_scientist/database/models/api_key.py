"""
API key model for REST API authentication.

API keys allow programmatic access to the OpenScientist REST API.
Users can create multiple API keys with descriptive names.
"""

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
    """
    API key for REST API authentication.

    API keys use the format "name:secret" where:
    - name: User-chosen descriptive name (stored plaintext)
    - secret: Cryptographically random token (stored hashed)

    The full "name:secret" key is only shown once at creation time.

    Attributes:
        user_id: Foreign key to the user who owns this API key
        name: User-chosen descriptive name for the key
        key_hash: Hashed secret portion of the API key
        last_used_at: Timestamp when key was last used (UTC)
        is_active: Whether the key is active (for revocation)
        usage_count: Number of times this API key has been used
        user: Related User object
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

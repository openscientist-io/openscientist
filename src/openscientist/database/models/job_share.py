"""Job sharing model for multi-user collaboration."""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job
    from .user import User


class JobShare(UUIDv7Mixin, Base):
    """Job sharing permission."""

    __tablename__ = "job_shares"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "shared_with_user_id",
            name="uq_job_share_user",
        ),
        CheckConstraint(
            "permission_level IN ('view', 'edit')",
            name="ck_job_share_permission",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job being shared",
    )

    shared_with_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User being granted access",
    )

    permission_level: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Permission level (view/edit)",
    )

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="shares")

    shared_with_user: Mapped["User"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<JobShare(id={self.id}, job_id={self.job_id}, "
            f"shared_with_user_id={self.shared_with_user_id}, "
            f"permission={self.permission_level})>"
        )

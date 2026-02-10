"""
Job sharing model for multi-user collaboration.

Job shares allow users to grant view or edit access to their jobs.
Permissions are enforced at the database level via Row-Level Security.
"""

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
    """
    Job sharing permission.

    Grants a user access to another user's job with a specific permission level.

    Permission levels:
    - 'view': Read-only access to job and results
    - 'edit': Can modify job parameters and cancel/resume

    Attributes:
        job_id: Foreign key to the shared job
        shared_with_user_id: Foreign key to user being granted access
        permission_level: Permission level ('view' or 'edit')
        job: Related Job object
        shared_with_user: Related User object
    """

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

"""The admin settings row and its change history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

_BUDGET_COLUMNS = (
    "max_job_cost_usd",
    "max_project_spend_total_usd",
    "max_project_spend_24h_usd",
    "warn_project_spend_24h_usd",
)


class AdminSettings(Base):
    """The one row holding the runtime policy an admin edits at run time."""

    __tablename__ = "admin_settings"

    __table_args__ = (
        CheckConstraint("id", name="ck_admin_settings_id"),
        *[
            CheckConstraint(f"{column} > 0", name=f"ck_admin_settings_{column}")
            for column in (
                *_BUDGET_COLUMNS,
                "agent_cpu",
                "executor_cpu",
                "agent_memory_mib",
                "executor_memory_mib",
                "agent_timeout_seconds",
                "executor_timeout_seconds",
                "max_file_size_mb",
                "scheduler_window_hours",
            )
        ],
        CheckConstraint("max_concurrent_jobs >= 0", name="ck_admin_settings_max_concurrent_jobs"),
        CheckConstraint(
            "session_duration_days >= 1", name="ck_admin_settings_session_duration_days"
        ),
    )

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))

    # NULL means no limit.
    max_job_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_project_spend_total_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_project_spend_24h_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    warn_project_spend_24h_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    agent_cpu: Mapped[float] = mapped_column(Float, nullable=False)
    executor_cpu: Mapped[float] = mapped_column(Float, nullable=False)
    agent_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_file_size_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduler_window_hours: Mapped[float] = mapped_column(Float, nullable=False)
    airgapped: Mapped[bool] = mapped_column(Boolean, nullable=False)
    session_duration_days: Mapped[int] = mapped_column(Integer, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<AdminSettings(airgapped={self.airgapped})>"


class AdminSettingChange(Base):
    """One recorded edit of the admin settings."""

    __tablename__ = "admin_setting_changes"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    changed_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    setting: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<AdminSettingChange(setting={self.setting!r})>"

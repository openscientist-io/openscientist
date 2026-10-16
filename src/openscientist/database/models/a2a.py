"""A2A admission control and durable associations with ordinary jobs."""

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class A2ASettings(Base):
    """One deployment-wide switch, writable only by administrators."""

    __tablename__ = "a2a_settings"
    __table_args__ = (CheckConstraint("id = 1", name="a2a_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class A2ATask(Base):
    """One A2A task per job; terminal projections remain stable after UI retries."""

    __tablename__ = "a2a_tasks"

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    message: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    final_task: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

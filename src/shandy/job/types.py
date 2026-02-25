"""
Shared types for SHANDY job management.

JobStatus, JobInfo, and JobStatusUpdateResult live here to avoid circular
imports between job/lifecycle.py and job_manager.py.

job_manager.py also re-exports these to preserve a stable import surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    """Job status enum matching database schema."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_FEEDBACK = "awaiting_feedback"
    GENERATING_REPORT = "generating_report"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobStatusUpdateResult:
    """Result of updating job status, includes data needed for notifications."""

    ntfy_enabled: bool = False
    ntfy_topic: str | None = None


@dataclass
class JobInfo:
    """Job information for API/UI responses."""

    job_id: str
    research_question: str
    status: JobStatus
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    max_iterations: int = 10
    iterations_completed: int = 0
    findings_count: int = 0
    error: str | None = None
    cancellation_reason: str | None = None
    use_hypotheses: bool = False
    investigation_mode: str = "autonomous"
    owner_id: str | None = None
    short_title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobInfo:
        data = data.copy()
        data["status"] = JobStatus(data["status"])
        return cls(**data)

    @classmethod
    def from_db_model(
        cls,
        job: Any,
        iterations_completed: int = 0,
        findings_count: int = 0,
    ) -> JobInfo:
        return cls(
            job_id=str(job.id),
            research_question=job.title,
            status=JobStatus(job.status),
            created_at=job.created_at.isoformat(),
            started_at=(
                job.updated_at.isoformat()
                if job.status
                in {
                    JobStatus.RUNNING,
                    JobStatus.AWAITING_FEEDBACK,
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                }
                else None
            ),
            completed_at=(
                job.updated_at.isoformat() if job.status == JobStatus.COMPLETED else None
            ),
            failed_at=job.updated_at.isoformat() if job.status == JobStatus.FAILED else None,
            max_iterations=job.max_iterations,
            iterations_completed=iterations_completed,
            findings_count=findings_count,
            error=job.error_message,
            cancellation_reason=job.cancellation_reason,
            use_hypotheses=bool(getattr(job, "use_hypotheses", False)),
            investigation_mode=getattr(job, "investigation_mode", "autonomous"),
            owner_id=str(job.owner_id) if job.owner_id else None,
            short_title=job.short_title,
        )

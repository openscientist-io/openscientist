"""
Shared types for SHANDY job management.

JobStatus, JobInfo, and JobStatusUpdateResult moved here from job_manager.py
to avoid circular imports between job/lifecycle.py and job_manager.py.

job_manager.py re-exports these for backward compatibility.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional


class JobStatus(str, Enum):
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
    ntfy_topic: Optional[str] = None


@dataclass
class JobInfo:
    """Job information for API/UI responses."""

    job_id: str
    research_question: str
    status: JobStatus
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    failed_at: Optional[str] = None
    max_iterations: int = 10
    iterations_completed: int = 0
    findings_count: int = 0
    error: Optional[str] = None
    cancellation_reason: Optional[str] = None
    use_skills: bool = True
    investigation_mode: str = "autonomous"
    owner_id: Optional[str] = None
    short_title: Optional[str] = None

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
                if job.status in ["running", "completed", "failed"]
                else None
            ),
            completed_at=(job.updated_at.isoformat() if job.status == "completed" else None),
            failed_at=job.updated_at.isoformat() if job.status == "failed" else None,
            max_iterations=job.max_iterations,
            iterations_completed=iterations_completed,
            findings_count=findings_count,
            error=job.error_message,
            cancellation_reason=job.cancellation_reason,
            use_skills=True,
            investigation_mode="autonomous",
            owner_id=str(job.owner_id) if job.owner_id else None,
            short_title=job.short_title,
        )

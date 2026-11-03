"""The guarded status UPDATE shared by both writers of a job's status."""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import func, update

from openscientist.database.models.job import Job as JobModel
from openscientist.job.types import JobStatus

# JobStatus is a StrEnum, so these member sets match the container's string statuses too.
#: Statuses after which a job holds no slot, so the scheduler stops counting it.
_ENDED_STATUSES: frozenset[JobStatus | str] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)
#: Statuses that end the agent's current work, so its status label goes with it.
#: The report phase starts new work, so the last iteration's label goes too.
_LABEL_ENDING_STATUSES: frozenset[JobStatus | str] = _ENDED_STATUSES | {
    JobStatus.AWAITING_FEEDBACK,
    JobStatus.GENERATING_REPORT,
}


def _value(status: JobStatus | str) -> str:
    return status.value if isinstance(status, JobStatus) else status


def status_update_statement(
    job_uuid: UUID,
    status: JobStatus | str,
    *,
    allowed_from: Collection[JobStatus | str],
    error_message: str | None = None,
    cancellation_reason: str | None = None,
    stop_note: str | None = None,
    takes_slot: bool = False,
    stamps_end: bool = False,
):
    """The guarded UPDATE moving the job to ``status`` if it is in one of
    ``allowed_from``; ``takes_slot`` stamps the slot start, ``stamps_end``
    stamps the slot end, and each caller adds its own ``.returning(...)``.
    The container's writer passes neither, so its writes never touch the
    times the web owns."""
    values: dict[str, object] = {"status": _value(status)}
    if error_message:
        values["error_message"] = error_message
    if cancellation_reason:
        values["cancellation_reason"] = cancellation_reason
    if stop_note:
        values["stop_note"] = stop_note
    if takes_slot:
        values["started_at"] = func.now()
        values["ended_at"] = None
    elif stamps_end and status in _ENDED_STATUSES:
        values["ended_at"] = func.now()
    if status in _LABEL_ENDING_STATUSES:
        values["agent_status"] = None
        values["agent_status_updated_at"] = None
    return (
        update(JobModel)
        .where(
            JobModel.id == job_uuid,
            JobModel.status.in_([_value(allowed) for allowed in allowed_from]),
        )
        .values(**values)
    )

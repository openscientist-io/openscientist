"""
ContainerMonitor — polls the database for job terminal status.

For each container-based job, a ContainerMonitor asyncio task:
- Polls `SELECT status FROM jobs WHERE id = :job_id` every 5 seconds
- When status becomes terminal (completed/failed/cancelled), calls the
  on_terminal callback which cleans up the container and starts the
  next queued job
- Enforces a hard timeout (default 4 hours) and marks the job failed

The web server reads status ONLY from PostgreSQL.  The agent container
updates status via the standard _db_update_job_status() path.

Usage::

    monitor = ContainerMonitor(
        job_id=job_id,
        on_terminal=lambda jid, status: ...,
        timeout_hours=4,
    )
    monitor.start()          # fire-and-forget asyncio task
    await monitor.cancel()   # if we need to stop early
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
POLL_INTERVAL_SECONDS = 5


class ContainerMonitor:
    """Monitors a single container-based job's status via database polling."""

    def __init__(
        self,
        job_id: str,
        on_terminal: Callable[[str, str], None],
        timeout_hours: int = 4,
    ) -> None:
        self._job_id = job_id
        self._on_terminal = on_terminal
        self._timeout_hours = timeout_hours
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the monitoring asyncio task (non-blocking)."""
        self._task = asyncio.create_task(self._run(), name=f"container-monitor-{self._job_id[:8]}")
        logger.info("ContainerMonitor started for job %s", self._job_id)

    async def cancel(self) -> None:
        """Cancel the monitoring task."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ContainerMonitor cancelled for job %s", self._job_id)

    async def _run(self) -> None:
        """Main polling loop."""
        timeout_seconds = self._timeout_hours * 3600
        elapsed = 0

        while elapsed < timeout_seconds:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

            try:
                status = await self._fetch_status()
            except Exception as e:
                logger.warning("ContainerMonitor: DB poll failed for job %s: %s", self._job_id, e)
                continue

            if status in TERMINAL_STATUSES:
                logger.info(
                    "ContainerMonitor: job %s reached terminal status=%s",
                    self._job_id,
                    status,
                )
                self._on_terminal(self._job_id, status)
                return

        # Hard timeout
        logger.error(
            "ContainerMonitor: job %s timed out after %d hours — marking failed",
            self._job_id,
            self._timeout_hours,
        )
        try:
            await self._mark_timed_out()
        except Exception as e:
            logger.error("Failed to mark timed-out job %s: %s", self._job_id, e)

        self._on_terminal(self._job_id, "failed")

    async def _fetch_status(self) -> str:
        """Query the database for the current job status."""
        from uuid import UUID

        from sqlalchemy import select

        from shandy.database.models.job import Job as JobModel
        from shandy.database.session import AsyncSessionLocal

        async with AsyncSessionLocal(thread_safe=False) as session:
            result = await session.execute(
                select(JobModel.status).where(JobModel.id == UUID(self._job_id))
            )
            row = result.scalar_one_or_none()
            return row or "unknown"

    async def _mark_timed_out(self) -> None:
        """Set job status to failed with a timeout error message."""
        from uuid import UUID

        from sqlalchemy import update as sa_update

        from shandy.database.models.job import Job as JobModel
        from shandy.database.session import AsyncSessionLocal

        async with AsyncSessionLocal(thread_safe=False) as session:
            await session.execute(
                sa_update(JobModel)
                .where(JobModel.id == UUID(self._job_id))
                .values(
                    status="failed",
                    error_message=f"Job timed out after {self._timeout_hours} hours",
                )
            )
            await session.commit()

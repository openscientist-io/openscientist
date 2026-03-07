"""
Job manager for OpenScientist discovery jobs.

Handles job lifecycle, status tracking, and cleanup.
"""

import argparse
import json
import logging
import shutil
import threading
import time
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.container_manager import get_container_manager
from openscientist.database.models import User
from openscientist.database.models.job import Job as JobModel
from openscientist.database.models.job_share import JobShare
from openscientist.database.rls import set_current_user
from openscientist.database.session import AsyncSessionLocal
from openscientist.exceptions import ProviderError
from openscientist.job.types import JobInfo, JobStatus, JobStatusUpdateResult
from openscientist.knowledge_state import KnowledgeState
from openscientist.ntfy import notify_job_status_change
from openscientist.orchestrator import create_job
from openscientist.orchestrator import regenerate_report as _regenerate_report
from openscientist.providers import get_provider
from openscientist.version import get_version_string

logger = logging.getLogger(__name__)


# Database helper functions for async operations


async def _apply_rls_context(session: AsyncSession, user_id: UUID | None) -> None:
    """Apply user-scoped RLS context when a user id is provided."""
    if user_id is None:
        return
    await session.execute(text("SET ROLE openscientist_app"))
    await set_current_user(session, user_id)


def _load_progress_from_knowledge_state(
    job_id: str,
    status: str,
    default_iterations: int,
    default_findings: int,
) -> tuple[int, int]:
    """Load iteration and findings progress from persisted knowledge state."""
    try:
        ks = KnowledgeState.load_from_database_sync(job_id).data
        findings_count = len(ks.get("findings", []))
        ks_iteration = int(ks.get("iteration", 1))

        if status in ("running", "awaiting_feedback"):
            iterations_completed = ks_iteration - 1 if ks_iteration > 1 else 0
        else:
            iterations_completed = ks_iteration

        return iterations_completed, findings_count
    except Exception as e:
        logger.warning("Failed to load KS for job %s: %s", job_id, e)
        return default_iterations, default_findings


async def _db_create_job(
    job_id: str,
    research_question: str,
    max_iterations: int,
    use_hypotheses: bool = False,
    investigation_mode: str = "autonomous",
    owner_id: UUID | None = None,
    title: str | None = None,
    description: str | None = None,
    pdb_code: str | None = None,
    space_group: str | None = None,
) -> JobModel:
    """Create a job in the database (thread-safe for worker threads).

    Args:
        job_id: UUID string for the job (used as primary key)
        research_question: The research question/title
        max_iterations: Maximum iterations allowed
        use_hypotheses: Whether hypothesis tracking tools are enabled for this job
        investigation_mode: Investigation mode ('autonomous' or 'coinvestigate')
        owner_id: UUID of the job owner (optional)
        title: Display title for the job (defaults to research_question)
        description: Optional job description
        pdb_code: Optional PDB code
        space_group: Optional crystal space group

    Returns:
        The created JobModel instance
    """
    async with AsyncSessionLocal(thread_safe=True) as session:
        await _apply_rls_context(session, owner_id)

        job = JobModel(
            id=UUID(job_id),
            owner_id=owner_id,
            title=title or research_question,
            description=description,
            use_hypotheses=use_hypotheses,
            investigation_mode=investigation_mode,
            status=JobStatus.PENDING.value,
            max_iterations=max_iterations,
            current_iteration=0,
            pdb_code=pdb_code,
            space_group=space_group,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job


async def _db_get_job(job_id: str, user_id: UUID | None = None) -> JobModel | None:
    """Get a job from the database (thread-safe for worker threads).

    When user_id is provided, drops to openscientist_app role so RLS policies
    are enforced. Without user_id, runs as the connection user (superuser)
    which bypasses RLS — use only for internal/system operations.
    """
    async with AsyncSessionLocal(thread_safe=True) as session:
        await _apply_rls_context(session, user_id)

        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _db_get_share_permission(job_id: str, user_id: UUID) -> str | None:
    """Return the share permission level for a user on a job.

    Returns 'view', 'edit', or None if the user has no share.
    """
    async with AsyncSessionLocal(thread_safe=True) as session:
        await _apply_rls_context(session, user_id)

        stmt = select(JobShare.permission_level).where(
            JobShare.job_id == UUID(job_id),
            JobShare.shared_with_user_id == user_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _db_update_job_status(
    job_id: str,
    status: JobStatus,
    error_message: str | None = None,
    user_id: UUID | None = None,
    cancellation_reason: str | None = None,
) -> JobStatusUpdateResult:
    """Update job status in the database (thread-safe for worker threads).

    Returns:
        JobStatusUpdateResult with the job owner's ntfy settings for notifications.
    """
    result = JobStatusUpdateResult()

    async with AsyncSessionLocal(thread_safe=True) as session:
        await _apply_rls_context(session, user_id)

        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        db_result = await session.execute(stmt)
        job = db_result.scalar_one_or_none()

        if job:
            job.status = status.value
            if error_message:
                job.error_message = error_message
            if cancellation_reason:
                job.cancellation_reason = cancellation_reason
            await session.commit()

            # Fetch owner's ntfy settings for notifications
            if job.owner_id:
                user_stmt = select(User.ntfy_enabled, User.ntfy_topic).where(
                    User.id == job.owner_id
                )
                user_result = await session.execute(user_stmt)
                user_row = user_result.first()
                if user_row:
                    result.ntfy_enabled = user_row.ntfy_enabled
                    result.ntfy_topic = user_row.ntfy_topic

    return result


async def _db_list_jobs(
    status: JobStatus | None = None,
    limit: int | None = None,
    user_id: UUID | None = None,
) -> list[JobModel]:
    """List jobs from the database (thread-safe for worker threads)."""
    async with AsyncSessionLocal(thread_safe=True) as session:
        await _apply_rls_context(session, user_id)

        stmt = select(JobModel).order_by(JobModel.created_at.desc())

        if status:
            stmt = stmt.where(JobModel.status == status.value)

        if limit:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _db_delete_job(job_id: str, user_id: UUID | None = None) -> None:
    """Delete a job from the database (thread-safe for worker threads)."""
    async with AsyncSessionLocal(thread_safe=True) as session:
        await _apply_rls_context(session, user_id)

        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        result = await session.execute(stmt)
        job = result.scalar_one_or_none()

        if job:
            await session.delete(job)
            await session.commit()


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run async code from sync context.

    Thin wrapper around :func:`openscientist.async_tasks.run_sync` kept for
    internal compatibility within this module.
    """
    from openscientist.async_tasks import run_sync

    return run_sync(coro)


class JobManager:
    """
    Manages OpenScientist discovery jobs.

    Features:
    - Create and queue jobs
    - Run jobs asynchronously
    - Track job status
    - List and query jobs
    - Clean up old jobs
    """

    def __init__(self, jobs_dir: Path = Path("jobs"), max_concurrent: int = 1):
        """
        Initialize job manager.

        Args:
            jobs_dir: Base directory for jobs
            max_concurrent: Maximum concurrent jobs
        """
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        self.max_concurrent = max_concurrent
        self._running_jobs: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

        # Clean up any stale running/queued jobs from previous restart
        self._cleanup_stale_jobs()

        logger.info(
            "JobManager initialized: %s, max_concurrent=%d",
            self.jobs_dir,
            max_concurrent,
        )

    def _cleanup_stale_jobs(self) -> None:
        """
        Mark any running/queued/awaiting_feedback jobs as cancelled on startup.

        This handles the case where the server restarts while jobs are in progress.
        Since the orchestrator process died, these jobs will never complete.
        """
        stale_count = 0
        for job_info in self._list_operational_jobs():
            job_id = job_info.job_id
            if job_info.status in [
                JobStatus.RUNNING,
                JobStatus.QUEUED,
                JobStatus.AWAITING_FEEDBACK,
                JobStatus.GENERATING_REPORT,
            ]:
                logger.warning(
                    "Marking stale job %s as cancelled (was %s)",
                    job_id,
                    job_info.status.value,
                )
                # Update database
                try:
                    owner_id = UUID(job_info.owner_id) if job_info.owner_id else None
                    _run_async(
                        _db_update_job_status(
                            job_id,
                            JobStatus.CANCELLED,
                            user_id=owner_id,
                            cancellation_reason="Server restarted while job was running",
                        )
                    )
                except Exception as e:
                    logger.error("Failed to update job %s in database: %s", job_id, e)

                # Stop any orphaned agent container for this stale job.
                try:
                    from openscientist.job_container import JobContainerRunner

                    JobContainerRunner().cleanup(job_id)
                except Exception as e:
                    logger.warning("Failed to cleanup container for stale job %s: %s", job_id, e)

                stale_count += 1

        if stale_count > 0:
            logger.info("Cleaned up %d stale job(s) from previous run", stale_count)

    def _ensure_job_not_exists(self, job_id: str) -> None:
        if self.get_job(job_id) is not None:
            raise ValueError(f"Job {job_id} already exists")

    def _check_budget_before_creation(self) -> None:
        try:
            provider = get_provider()
            budget_check = provider.check_budget_limits()
        except ProviderError as e:
            # Keep job creation available if provider cost endpoint is temporarily unavailable.
            logger.warning("Budget check unavailable: %s", e)
            budget_check = {"can_proceed": True}

        if budget_check.get("can_proceed", True):
            return

        errors = budget_check.get("errors", [])
        error_msg = "; ".join(errors) if errors else "Budget limit exceeded"
        raise ValueError(f"Cannot create job: {error_msg}")

    def _create_db_job_record(
        self,
        *,
        job_id: str,
        research_question: str,
        max_iterations: int,
        use_hypotheses: bool,
        investigation_mode: str,
        owner_id: str | None,
        title: str | None,
        description: str | None,
        pdb_code: str | None,
        space_group: str | None,
    ) -> UUID | None:
        owner_uuid = UUID(owner_id) if owner_id else None
        try:
            _run_async(
                _db_create_job(
                    job_id,
                    research_question,
                    max_iterations,
                    use_hypotheses=use_hypotheses,
                    investigation_mode=investigation_mode,
                    owner_id=owner_uuid,
                    title=title,
                    description=description,
                    pdb_code=pdb_code,
                    space_group=space_group,
                )
            )
        except Exception as e:
            logger.error("Failed to create job in database: %s", e)
            raise ValueError(f"Failed to create job in database: {e}") from e
        return owner_uuid

    def _rollback_failed_job_creation(self, job_id: str, owner_uuid: UUID | None) -> None:
        try:
            _run_async(_db_delete_job(job_id, owner_uuid))
        except Exception as cleanup_error:
            logger.error("Failed to rollback DB job %s: %s", job_id, cleanup_error)
        try:
            job_dir = self.jobs_dir / job_id
            if job_dir.exists():
                shutil.rmtree(job_dir)
        except OSError:
            logger.warning("Failed to cleanup job directory after failure: %s", job_id)

    def _create_job_files(
        self,
        *,
        job_id: str,
        research_question: str,
        data_files: list[Path],
        max_iterations: int,
        owner_id: str | None,
        owner_uuid: UUID | None,
    ) -> None:
        try:
            create_job(
                job_id=job_id,
                research_question=research_question,
                data_files=data_files,
                max_iterations=max_iterations,
                jobs_dir=self.jobs_dir,
                owner_id=owner_id,
            )
        except Exception as e:
            logger.error("Failed to initialize filesystem for job %s: %s", job_id, e)
            # Compensating action: remove partially-created DB row/files to avoid split-brain.
            self._rollback_failed_job_creation(job_id, owner_uuid)
            raise ValueError(f"Failed to initialize job files: {e}") from e

    def create_job(
        self,
        job_id: str,
        research_question: str,
        data_files: list[Path],
        max_iterations: int = 10,
        use_hypotheses: bool = False,
        auto_start: bool = True,
        investigation_mode: str = "autonomous",
        owner_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        pdb_code: str | None = None,
        space_group: str | None = None,
    ) -> JobInfo:
        """
        Create a new discovery job.

        Args:
            job_id: Unique job identifier
            research_question: Research question
            data_files: List of data file paths (can be empty for literature-only jobs)
            max_iterations: Maximum iterations
            use_hypotheses: Whether to enable hypothesis tracking tools
            auto_start: Whether to start job immediately
            investigation_mode: "autonomous" (default) or "coinvestigate"
            owner_id: UUID of the job owner (optional, for orphaned jobs)
            title: Display title for UI/API responses (defaults to research_question)
            description: Optional job description
            pdb_code: Optional PDB code metadata
            space_group: Optional crystal space group metadata

        Returns:
            JobInfo object

        Raises:
            ValueError: If job_id already exists
            BudgetExceededError: If insufficient budget
        """
        self._ensure_job_not_exists(job_id)
        self._check_budget_before_creation()

        # Create job in database
        logger.info("Creating job %s in database", job_id)
        owner_uuid = self._create_db_job_record(
            job_id=job_id,
            research_question=research_question,
            max_iterations=max_iterations,
            use_hypotheses=use_hypotheses,
            investigation_mode=investigation_mode,
            owner_id=owner_id,
            title=title,
            description=description,
            pdb_code=pdb_code,
            space_group=space_group,
        )

        # Create job directory and files
        logger.info("Creating job %s filesystem structure", job_id)
        self._create_job_files(
            job_id=job_id,
            research_question=research_question,
            data_files=data_files,
            max_iterations=max_iterations,
            owner_id=owner_id,
            owner_uuid=owner_uuid,
        )

        # Load job info
        job_info = self._load_job_info(job_id)
        if job_info is None:
            raise ValueError(f"Failed to load newly created job {job_id}")

        # Auto-start if requested
        if auto_start:
            self.start_job(job_id)

        return job_info

    def start_job(self, job_id: str) -> None:
        """
        Start a job asynchronously.

        Args:
            job_id: Job ID

        Raises:
            ValueError: If job not found or already running
        """
        with self._lock:
            # Check job exists
            job_info = self.get_job(job_id)
            if job_info is None:
                raise ValueError(f"Job {job_id} not found")

            # Check if already running
            if job_id in self._running_jobs:
                raise ValueError(f"Job {job_id} is already running")

            # Check concurrent limit (awaiting_feedback jobs don't count)
            if self._get_active_job_count() >= self.max_concurrent:
                # Queue the job
                self._update_job_status(job_id, JobStatus.QUEUED)
                logger.info("Job %s queued (max concurrent reached)", job_id)
                return

            # Start the job
            logger.info("Starting job %s", job_id)
            thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
            self._running_jobs[job_id] = thread
            thread.start()

    def _run_job(self, job_id: str) -> None:
        """Run a job (internal, called by thread)."""
        self._run_job_in_container(job_id)

    def _run_job_in_container(self, job_id: str) -> None:
        """Launch an agent container for the job and block until it reaches a terminal status."""
        from openscientist.job_container import JobContainerRunner

        poll_interval = 5
        terminal_statuses = {"completed", "failed", "cancelled"}

        job_dir = self.jobs_dir / job_id
        runner = JobContainerRunner()

        try:
            self._update_job_status(job_id, JobStatus.RUNNING)
            runner.launch(job_id, job_dir)
            logger.info("Agent container launched for job %s", job_id)

            # Poll the database until the container's agent writes a terminal status.
            # Also check if the container has exited unexpectedly (crash before DB write).
            timeout_seconds = 4 * 3600
            elapsed = 0
            while elapsed < timeout_seconds:
                time.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    job_info = self._load_job_info(job_id)
                    if job_info and job_info.status.value in terminal_statuses:
                        logger.info(
                            "Container job %s reached terminal status: %s",
                            job_id,
                            job_info.status.value,
                        )
                        return
                except Exception as poll_err:
                    logger.warning("DB poll failed for job %s: %s", job_id, poll_err)

                # If the container has exited but the DB still shows running,
                # the agent crashed before writing a terminal status — fail fast.
                exit_code = runner.get_exit_code(job_id)
                if exit_code is not None and exit_code != 0:
                    logger.error(
                        "Agent container for job %s exited with code %d before writing terminal status",
                        job_id,
                        exit_code,
                    )
                    self._update_job_status(
                        job_id,
                        JobStatus.FAILED,
                        error_message=f"Agent container exited with code {exit_code}",
                    )
                    return

            # Hard timeout reached.
            logger.error("Container job %s timed out after 4 hours", job_id)
            self._update_job_status(
                job_id, JobStatus.FAILED, error_message="Job timed out after 4 hours"
            )

        except Exception as e:
            logger.error(
                "Container job %s failed [%s]: %s", job_id, get_version_string(), e, exc_info=True
            )
            self._update_job_status(job_id, JobStatus.FAILED, error_message=str(e))

        finally:
            runner.cleanup(job_id, log_dir=job_dir)
            with self._lock:
                self._running_jobs.pop(job_id, None)
            self._start_next_queued_job()

    def regenerate_report(self, job_id: str) -> None:
        """
        Re-run report generation for a completed or failed job.

        Validates that the job exists and is in a terminal state.
        Spawns a background thread.

        Args:
            job_id: Job ID

        Raises:
            ValueError: If job not found or not in valid state
        """
        job_info = self.get_job(job_id)
        if job_info is None:
            raise ValueError(f"Job {job_id} not found")

        if job_info.status not in [JobStatus.COMPLETED, JobStatus.FAILED]:
            raise ValueError(
                f"Can only regenerate report for completed or failed jobs "
                f"(current status: {job_info.status.value})"
            )

        with self._lock:
            if job_id in self._running_jobs:
                raise ValueError(f"Job {job_id} already has an operation in progress")

            thread = threading.Thread(
                target=self._run_regenerate_report, args=(job_id,), daemon=True
            )
            self._running_jobs[job_id] = thread
            thread.start()

    def _run_regenerate_report(self, job_id: str) -> None:
        """
        Run report regeneration (internal, called by thread).

        Args:
            job_id: Job ID
        """
        job_dir = self.jobs_dir / job_id

        try:
            self._update_job_status(job_id, JobStatus.GENERATING_REPORT)

            logger.info("Regenerating report for job %s", job_id)
            result = _regenerate_report(job_dir)

            logger.info("Report regeneration for job %s completed: %s", job_id, result)

            final_status = result.get("status", "completed")
            if final_status == "completed":
                self._update_job_status(job_id, JobStatus.COMPLETED)
            elif final_status == "failed":
                self._update_job_status(job_id, JobStatus.FAILED, error_message=result.get("error"))

        except Exception as e:
            logger.error(
                "Report regeneration for job %s failed [%s]: %s",
                job_id,
                get_version_string(),
                e,
                exc_info=True,
            )
            self._update_job_status(job_id, JobStatus.FAILED, error_message=str(e))

        finally:
            with self._lock:
                self._running_jobs.pop(job_id, None)

    def _start_next_queued_job(self) -> None:
        """Start the next queued job if slots available."""
        with self._lock:
            if self._get_active_job_count() >= self.max_concurrent:
                return

            queued_jobs = self._list_operational_jobs(status=JobStatus.QUEUED, limit=1)
            if not queued_jobs:
                return

            job_id = queued_jobs[0].job_id
            logger.info("Starting queued job %s", job_id)
            thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
            self._running_jobs[job_id] = thread
            thread.start()

    def cancel_job(self, job_id: str) -> None:
        """
        Cancel a pending, running, or queued job.

        Args:
            job_id: Job ID

        Raises:
            ValueError: If job not found
        """
        job_info = self.get_job(job_id)
        if job_info is None:
            raise ValueError(f"Job {job_id} not found")

        if job_info.status not in [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.QUEUED]:
            raise ValueError(f"Job {job_id} is not pending, running, or queued")

        # Update status with reason
        self._update_job_status(
            job_id, JobStatus.CANCELLED, cancellation_reason="Cancelled by user"
        )

        # For running jobs, keep the thread tracked until it exits so
        # active-slot accounting stays accurate.
        with self._lock:
            if job_info.status in [JobStatus.PENDING, JobStatus.QUEUED]:
                self._running_jobs.pop(job_id, None)

        # Send SIGTERM to the agent container immediately so it doesn't
        # keep burning resources until the polling loop notices the DB change.
        if job_info.status == JobStatus.RUNNING:
            try:
                from openscientist.job_container import JobContainerRunner

                JobContainerRunner().stop(job_id)
            except Exception as e:
                logger.warning("Failed to stop container for cancelled job %s: %s", job_id, e)

        logger.info("Job %s cancelled", job_id)

        # Start next queued job if any
        self._start_next_queued_job()

    def get_job(self, job_id: str) -> JobInfo | None:
        """
        Get job information.

        Args:
            job_id: Job ID

        Returns:
            JobInfo or None if not found
        """
        return self._load_job_info(job_id)

    def list_jobs(
        self,
        status: JobStatus | None = None,
        limit: int | None = None,
    ) -> list[JobInfo]:
        """
        List all jobs from the database (no user filtering).

        For user-facing queries, use a database session with RLS instead.

        Args:
            status: Filter by status
            limit: Maximum number of jobs to return

        Returns:
            List of JobInfo objects, sorted by created_at (newest first)
        """
        db_jobs = _run_async(_db_list_jobs(status=status, limit=limit))
        return [self._db_model_to_job_info(m) for m in db_jobs]

    def delete_job(self, job_id: str) -> None:
        """
        Delete a job and its files.

        Args:
            job_id: Job ID

        Raises:
            ValueError: If job not found or still running
        """
        job_info = self.get_job(job_id)
        if job_info is None:
            raise ValueError(f"Job {job_id} not found")

        if job_info.status == JobStatus.RUNNING:
            raise ValueError(f"Cannot delete running job {job_id}")

        # Delete from database
        try:
            owner_id = UUID(job_info.owner_id) if job_info.owner_id else None
            _run_async(_db_delete_job(job_id, owner_id))
        except Exception as e:
            logger.error("Failed to delete job from database: %s", e)
            raise ValueError(f"Failed to delete job {job_id} from database: {e}") from e

        # Clean up any executor containers for this job
        try:
            container_manager = get_container_manager()
            if container_manager.is_available():
                removed = container_manager.cleanup_job_containers(job_id)
                if removed > 0:
                    logger.info("Removed %d executor container(s) for job %s", removed, job_id)
        except Exception as e:
            logger.warning("Failed to cleanup containers for job %s: %s", job_id, e)

        # Delete job directory
        job_dir = self.jobs_dir / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)
            logger.info("Deleted job %s", job_id)

    def cleanup_old_jobs(self, days: int = 7, keep_completed: bool = True) -> int:
        """
        Clean up old jobs.

        Args:
            days: Delete jobs older than this many days
            keep_completed: Keep completed jobs regardless of age

        Returns:
            Number of jobs deleted
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        deleted = 0

        for job_info in self._list_operational_jobs():
            job_id = job_info.job_id

            # Skip running jobs
            if job_info.status == JobStatus.RUNNING:
                continue

            # Skip completed jobs if requested
            if keep_completed and job_info.status == JobStatus.COMPLETED:
                continue

            # Check age
            created_at = datetime.fromisoformat(job_info.created_at)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if created_at < cutoff:
                try:
                    self.delete_job(job_id)
                    deleted += 1
                except (OSError, ValueError) as e:
                    logger.error("Failed to delete job %s: %s", job_id, e)

        # Also cleanup orphaned executor containers
        try:
            container_manager = get_container_manager()
            if container_manager.is_available():
                orphaned = container_manager.cleanup_orphaned_containers(max_age_hours=days * 24)
                if orphaned > 0:
                    logger.info("Removed %d orphaned executor container(s)", orphaned)
        except Exception as e:
            logger.warning("Failed to cleanup orphaned containers: %s", e)

        logger.info("Cleaned up %d old jobs", deleted)
        return deleted

    def _get_active_job_count(self) -> int:
        """
        Get count of actively running jobs (excluding awaiting_feedback).

        Jobs in AWAITING_FEEDBACK status don't count against the concurrent limit
        so scientists can take unlimited time without blocking the queue.
        """
        count = 0
        for job_id in self._running_jobs:
            job_info = self.get_job(job_id)
            if job_info and job_info.status != JobStatus.AWAITING_FEEDBACK:
                count += 1
        return count

    def get_coinvestigate_count(self) -> int:
        """
        Get count of jobs in coinvestigate mode (running or awaiting feedback).

        Used to limit concurrent coinvestigations to prevent resource exhaustion.
        """
        count = 0
        for job_info in self._list_operational_jobs():
            if job_info.investigation_mode == "coinvestigate" and job_info.status in [
                JobStatus.RUNNING,
                JobStatus.AWAITING_FEEDBACK,
                JobStatus.QUEUED,
            ]:
                count += 1
        return count

    def can_start_coinvestigate(self, max_coinvestigate: int = 15) -> bool:
        """
        Check if a new coinvestigate job can be started.

        Args:
            max_coinvestigate: Maximum concurrent coinvestigate jobs (default 15)

        Returns:
            True if under the limit, False otherwise
        """
        return self.get_coinvestigate_count() < max_coinvestigate

    def get_job_summary(self) -> dict[str, Any]:
        """
        Get summary of all jobs (no user filtering).

        Returns:
            Dictionary with job counts and budget info
        """
        jobs = self.list_jobs()

        status_counts = {}
        for status in JobStatus:
            status_counts[status.value] = sum(1 for j in jobs if j.status == status)

        # Get project-level cost info from provider
        try:
            provider = get_provider()
            cost_info = provider.get_cost_info(lookback_hours=24)
            budget_check = provider.check_budget_limits()
        except (ValueError, ProviderError) as e:
            logger.warning("Could not fetch cost info: %s", e)
            cost_info = None
            budget_check = None

        return {
            "total_jobs": len(jobs),
            "status_counts": status_counts,
            "cost_info": cost_info,
            "budget_check": budget_check,
        }

    def _list_operational_jobs(
        self,
        status: JobStatus | None = None,
        limit: int | None = None,
    ) -> list[JobInfo]:
        """
        List jobs for operational workflows.

        Jobs are sourced exclusively from the database.
        """
        try:
            db_jobs = _run_async(_db_list_jobs(status=status, limit=limit))
            return [self._db_model_to_job_info(job) for job in db_jobs]
        except Exception as e:
            logger.warning("Failed to list jobs from database for operational scan: %s", e)
            return []

    def _db_model_to_job_info(self, job_model: JobModel) -> JobInfo:
        """Convert a database JobModel to JobInfo with real-time progress from KS."""
        # Load progress from persisted knowledge state for all jobs.
        job_id = str(job_model.id)
        iterations_completed, findings_count = _load_progress_from_knowledge_state(
            job_id=job_id,
            status=job_model.status,
            default_iterations=job_model.current_iteration,
            default_findings=0,
        )

        return JobInfo.from_db_model(job_model, iterations_completed, findings_count)

    def _load_job_info(self, job_id: str) -> JobInfo | None:
        """
        Load job info from database.
        """
        try:
            job_model = _run_async(_db_get_job(job_id))
            if job_model:
                return self._db_model_to_job_info(job_model)
        except Exception as e:
            logger.warning("Failed to load job %s from database: %s", job_id, e)
            return None

        return None

    def _update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        error_message: str | None = None,
        cancellation_reason: str | None = None,
    ) -> None:
        """Update job status in the database."""
        # Get job to find owner
        job_info = self._load_job_info(job_id)
        owner_id = UUID(job_info.owner_id) if job_info and job_info.owner_id else None

        # Update database and get ntfy settings
        ntfy_result: JobStatusUpdateResult | None = None
        try:
            ntfy_result = _run_async(
                _db_update_job_status(job_id, status, error_message, owner_id, cancellation_reason)
            )
        except Exception as e:
            logger.error("Failed to update job status in database: %s", e)

        # Send push notification if user has ntfy enabled
        # Note: notify_job_status_change will create topic if not exists
        if owner_id and job_info and ntfy_result and ntfy_result.ntfy_enabled:
            try:
                _run_async(
                    notify_job_status_change(
                        user_id=owner_id,
                        job_id=job_id,
                        job_title=job_info.research_question,
                        new_status=status.value,
                        error_message=error_message,
                        cancellation_reason=cancellation_reason,
                        iteration=job_info.iterations_completed,
                        ntfy_topic=ntfy_result.ntfy_topic,
                    )
                )
            except Exception as e:
                logger.warning("Failed to send ntfy notification: %s", e)


def main() -> None:
    """CLI entry point for job manager."""
    parser = argparse.ArgumentParser(description="OpenScientist Job Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # List jobs
    list_parser = subparsers.add_parser("list", help="List jobs")
    list_parser.add_argument("--status", help="Filter by status")
    list_parser.add_argument("--limit", type=int, help="Limit number of jobs")

    # Get job
    get_parser = subparsers.add_parser("get", help="Get job info")
    get_parser.add_argument("job_id", help="Job ID")

    # Delete job
    delete_parser = subparsers.add_parser("delete", help="Delete job")
    delete_parser.add_argument("job_id", help="Job ID")

    # Cleanup
    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up old jobs")
    cleanup_parser.add_argument("--days", type=int, default=7, help="Delete jobs older than N days")
    cleanup_parser.add_argument(
        "--delete-completed", action="store_true", help="Delete completed jobs too"
    )

    # Summary
    subparsers.add_parser("summary", help="Get job summary")

    # Bootstrap filesystem jobs into DB
    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Bootstrap filesystem jobs into the database",
    )
    bootstrap_parser.add_argument(
        "--jobs-dir",
        default="jobs",
        help="Directory containing job folders (default: jobs)",
    )
    bootstrap_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report without writing database changes",
    )

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.command == "bootstrap":
        from openscientist.bootstrap import bootstrap_jobs_from_filesystem_sync

        result = bootstrap_jobs_from_filesystem_sync(
            jobs_dir=Path(args.jobs_dir),
            dry_run=args.dry_run,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return

    # Create job manager
    manager = JobManager()

    # Execute command
    if args.command == "list":
        status = JobStatus(args.status) if args.status else None
        jobs = manager.list_jobs(status=status, limit=args.limit)

        print(f"{'Job ID':<20} {'Status':<12} {'Iterations':<12} {'Findings':<10} {'Created At'}")
        print("-" * 80)

        for job in jobs:
            print(
                f"{job.job_id:<20} {job.status.value:<12} "
                f"{job.iterations_completed}/{job.max_iterations:<6} "
                f"{job.findings_count:<10} {job.created_at}"
            )

    elif args.command == "get":
        job_result = manager.get_job(args.job_id)
        if job_result is None:
            print(f"Job {args.job_id} not found")
        else:
            print(json.dumps(job_result.to_dict(), indent=2))

    elif args.command == "delete":
        manager.delete_job(args.job_id)
        print(f"Deleted job {args.job_id}")

    elif args.command == "cleanup":
        deleted = manager.cleanup_old_jobs(days=args.days, keep_completed=not args.delete_completed)
        print(f"Deleted {deleted} jobs")

    elif args.command == "summary":
        summary = manager.get_job_summary()
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

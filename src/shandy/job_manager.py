"""
Job manager for SHANDY discovery jobs.

Handles job lifecycle, status tracking, and cleanup.
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import shutil
import threading
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TypeVar
from uuid import UUID

from sqlalchemy import select

from shandy.container_manager import get_container_manager
from shandy.database.models import User
from shandy.database.models.job import Job as JobModel
from shandy.database.rls import set_current_user
from shandy.database.session import AsyncSessionLocal
from shandy.exceptions import ProviderError
from shandy.job.types import JobInfo, JobStatus, JobStatusUpdateResult
from shandy.ntfy import ensure_user_has_topic, get_user_ntfy_settings, notify_job_status_change
from shandy.orchestrator import create_job, run_discovery
from shandy.orchestrator import regenerate_report as _regenerate_report
from shandy.providers import get_provider
from shandy.version import get_version_string

logger = logging.getLogger(__name__)


# Database helper functions for async operations


async def _db_create_job(
    job_id: str,
    research_question: str,
    max_iterations: int,
    owner_id: Optional[UUID] = None,
) -> JobModel:
    """Create a job in the database (thread-safe for worker threads).

    Args:
        job_id: UUID string for the job (used as primary key)
        research_question: The research question/title
        max_iterations: Maximum iterations allowed
        owner_id: UUID of the job owner (optional)

    Returns:
        The created JobModel instance
    """
    async with AsyncSessionLocal(thread_safe=True) as session:
        if owner_id:
            await set_current_user(session, owner_id)

        job = JobModel(
            id=UUID(job_id),
            owner_id=owner_id,
            title=research_question,
            description=None,
            status=JobStatus.PENDING.value,
            max_iterations=max_iterations,
            current_iteration=0,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job


async def _db_get_job(job_id: str, user_id: Optional[UUID] = None) -> Optional[JobModel]:
    """Get a job from the database (thread-safe for worker threads)."""
    async with AsyncSessionLocal(thread_safe=True) as session:
        if user_id:
            await set_current_user(session, user_id)

        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _db_update_job_status(
    job_id: str,
    status: JobStatus,
    error_message: Optional[str] = None,
    user_id: Optional[UUID] = None,
    cancellation_reason: Optional[str] = None,
) -> JobStatusUpdateResult:
    """Update job status in the database (thread-safe for worker threads).

    Returns:
        JobStatusUpdateResult with the job owner's ntfy settings for notifications.
    """
    result = JobStatusUpdateResult()

    async with AsyncSessionLocal(thread_safe=True) as session:
        if user_id:
            await set_current_user(session, user_id)

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
    status: Optional[JobStatus] = None,
    limit: Optional[int] = None,
    user_id: Optional[UUID] = None,
) -> list[JobModel]:
    """List jobs from the database (thread-safe for worker threads)."""
    async with AsyncSessionLocal(thread_safe=True) as session:
        if user_id:
            await set_current_user(session, user_id)

        stmt = select(JobModel).order_by(JobModel.created_at.desc())

        if status:
            stmt = stmt.where(JobModel.status == status.value)

        if limit:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _db_delete_job(job_id: str, user_id: Optional[UUID] = None) -> None:
    """Delete a job from the database (thread-safe for worker threads)."""
    async with AsyncSessionLocal(thread_safe=True) as session:
        if user_id:
            await set_current_user(session, user_id)

        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        result = await session.execute(stmt)
        job = result.scalar_one_or_none()

        if job:
            await session.delete(job)
            await session.commit()


_T = TypeVar("_T")


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Helper to run async code from sync context.

    When called from within a running event loop (e.g., NiceGUI handlers),
    this runs the coroutine in a separate thread with its own event loop
    and a fresh database session to avoid connection pool conflicts.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        # We're inside a running event loop - run in a separate thread
        # with a fresh event loop. The coroutine will create its own
        # database session via AsyncSessionLocal context manager.
        def run_in_thread() -> _T:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_in_thread)
            return future.result(timeout=30)
    else:
        # No running loop - create one
        return asyncio.run(coro)


class JobManager:
    """
    Manages SHANDY discovery jobs.

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
        for job_id in self._list_job_ids():
            job_info = self._load_job_info(job_id)
            if job_info is None:
                continue

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
                    _run_async(_db_update_job_status(job_id, JobStatus.CANCELLED, user_id=owner_id))
                except Exception as e:
                    logger.error("Failed to update job %s in database: %s", job_id, e)

                # Also update config.json for backward compatibility
                self._update_job_status_file(
                    job_id,
                    JobStatus.CANCELLED,
                    cancellation_reason="Server restarted while job was running",
                )
                stale_count += 1

        if stale_count > 0:
            logger.info("Cleaned up %d stale job(s) from previous run", stale_count)

    def create_job(
        self,
        job_id: str,
        research_question: str,
        data_files: list[Path],
        max_iterations: int = 10,
        use_skills: bool = True,
        auto_start: bool = True,
        investigation_mode: str = "autonomous",
        owner_id: Optional[str] = None,
    ) -> JobInfo:
        """
        Create a new discovery job.

        Args:
            job_id: Unique job identifier
            research_question: Research question
            data_files: List of data file paths (can be empty for literature-only jobs)
            max_iterations: Maximum iterations
            use_skills: Whether to use skills
            auto_start: Whether to start job immediately
            investigation_mode: "autonomous" (default) or "coinvestigate"
            owner_id: UUID of the job owner (optional, for orphaned jobs)

        Returns:
            JobInfo object

        Raises:
            ValueError: If job_id already exists
            BudgetExceededError: If insufficient budget
        """
        # Check if job exists
        if self.get_job(job_id) is not None:
            raise ValueError(f"Job {job_id} already exists")

        # Check budget limits
        try:
            provider = get_provider()
            budget_check = provider.check_budget_limits()

            if not budget_check["can_proceed"]:
                errors = budget_check.get("errors", [])
                error_msg = "; ".join(errors) if errors else "Budget limit exceeded"
                raise ValueError(f"Cannot create job: {error_msg}")
        except (ValueError, ProviderError) as e:
            logger.warning("Budget check failed: %s", e)

        # Create job in database
        logger.info("Creating job %s in database", job_id)
        owner_uuid = UUID(owner_id) if owner_id else None
        try:
            _run_async(_db_create_job(job_id, research_question, max_iterations, owner_uuid))
        except Exception as e:
            logger.error("Failed to create job in database: %s", e)
            raise ValueError(f"Failed to create job in database: {e}") from e

        # Get ntfy settings for the owner (for push notifications)
        ntfy_enabled = False
        ntfy_topic: Optional[str] = None
        if owner_uuid:
            try:
                ntfy_enabled, ntfy_topic = _run_async(get_user_ntfy_settings(owner_uuid))
                # Ensure user has a topic if ntfy is enabled
                if ntfy_enabled and not ntfy_topic:
                    ntfy_topic = _run_async(ensure_user_has_topic(owner_uuid))
            except Exception as e:
                logger.warning("Failed to get ntfy settings for user: %s", e)

        # Create job directory and files (for backward compatibility)
        logger.info("Creating job %s filesystem structure", job_id)
        create_job(
            job_id=job_id,
            research_question=research_question,
            data_files=data_files,
            max_iterations=max_iterations,
            use_skills=use_skills,
            jobs_dir=self.jobs_dir,
            investigation_mode=investigation_mode,
            owner_id=owner_id,
            ntfy_enabled=ntfy_enabled,
            ntfy_topic=ntfy_topic,
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
        """
        Run a job (internal, called by thread).

        Args:
            job_id: Job ID
        """
        job_dir = self.jobs_dir / job_id

        try:
            # Update status
            self._update_job_status(job_id, JobStatus.RUNNING)

            # Run discovery
            logger.info("Running discovery for job %s", job_id)
            result = run_discovery(job_dir)

            logger.info("Job %s completed: %s", job_id, result)

            # Update database status based on result
            final_status = result.get("status", "completed")
            if final_status == "completed":
                self._update_job_status(job_id, JobStatus.COMPLETED)
            elif final_status == "failed":
                self._update_job_status(job_id, JobStatus.FAILED, error_message=result.get("error"))

        except Exception as e:  # noqa: BLE001 — thread-level safety net
            logger.error("Job %s failed [%s]: %s", job_id, get_version_string(), e, exc_info=True)
            self._update_job_status(job_id, JobStatus.FAILED, error_message=str(e))

        finally:
            # Remove from running jobs
            with self._lock:
                self._running_jobs.pop(job_id, None)

            # Start next queued job if any
            self._start_next_queued_job()

    def regenerate_report(self, job_id: str) -> None:
        """
        Re-run report generation for a completed or failed job.

        Validates that the job exists, is in a terminal state, and has
        a knowledge_state.json. Spawns a background thread.

        Args:
            job_id: Job ID

        Raises:
            ValueError: If job not found, not in valid state, or missing KS
        """
        job_info = self.get_job(job_id)
        if job_info is None:
            raise ValueError(f"Job {job_id} not found")

        if job_info.status not in [JobStatus.COMPLETED, JobStatus.FAILED]:
            raise ValueError(
                f"Can only regenerate report for completed or failed jobs "
                f"(current status: {job_info.status.value})"
            )

        ks_path = self.jobs_dir / job_id / "knowledge_state.json"
        if not ks_path.exists():
            raise ValueError(
                f"Job {job_id} has no knowledge state — nothing to generate a report from"
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

        except Exception as e:  # noqa: BLE001 — thread-level safety net
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

            # Find next queued job
            for job_id in self._list_job_ids():
                job_info = self.get_job(job_id)
                if job_info and job_info.status == JobStatus.QUEUED:
                    logger.info("Starting queued job %s", job_id)
                    thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
                    self._running_jobs[job_id] = thread
                    thread.start()
                    break

    def cancel_job(self, job_id: str) -> None:
        """
        Cancel a running or queued job.

        Args:
            job_id: Job ID

        Raises:
            ValueError: If job not found
        """
        job_info = self.get_job(job_id)
        if job_info is None:
            raise ValueError(f"Job {job_id} not found")

        if job_info.status not in [JobStatus.RUNNING, JobStatus.QUEUED]:
            raise ValueError(f"Job {job_id} is not running or queued")

        # Update status with reason
        self._update_job_status(
            job_id, JobStatus.CANCELLED, cancellation_reason="Cancelled by user"
        )

        # Remove from running jobs if present
        with self._lock:
            self._running_jobs.pop(job_id, None)

        # Note: We can't actually kill the thread cleanly in Python
        # The orchestrator will check status and stop at next iteration
        logger.info("Job %s cancelled (will stop at next iteration)", job_id)

        # Start next queued job if any
        self._start_next_queued_job()

    def get_job(self, job_id: str) -> Optional[JobInfo]:
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
        status: Optional[JobStatus] = None,
        limit: Optional[int] = None,
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
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = 0

        for job_id in self._list_job_ids():
            job_info = self._load_job_info(job_id)
            if job_info is None:
                continue

            # Skip running jobs
            if job_info.status == JobStatus.RUNNING:
                continue

            # Skip completed jobs if requested
            if keep_completed and job_info.status == JobStatus.COMPLETED:
                continue

            # Check age
            created_at = datetime.fromisoformat(job_info.created_at)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
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
        for job_id in self._list_job_ids():
            job_info = self.get_job(job_id)
            if job_info and job_info.investigation_mode == "coinvestigate":
                if job_info.status in [
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

    def _list_job_ids(self) -> list[str]:
        """List all job IDs."""
        if not self.jobs_dir.exists():
            return []

        job_ids = []
        for item in self.jobs_dir.iterdir():
            if item.is_dir() and (item / "config.json").exists():
                job_ids.append(item.name)

        return job_ids

    def _db_model_to_job_info(self, job_model: JobModel) -> JobInfo:
        """Convert a database JobModel to JobInfo with real-time progress from KS."""
        iterations_completed = job_model.current_iteration
        findings_count = 0

        # Load progress from knowledge_state.json for all jobs
        # (database sync may be async and not yet committed)
        ks_path = self.jobs_dir / str(job_model.id) / "knowledge_state.json"
        if ks_path.exists():
            try:
                with open(ks_path, encoding="utf-8") as f:
                    ks = json.load(f)
                findings_count = len(ks.get("findings", []))
                ks_iteration = ks.get("iteration", 1)

                # For running jobs, iteration is the current (in-progress) iteration
                # so completed = iteration - 1
                # For completed/failed/cancelled jobs, iteration is the final count
                if job_model.status in ("running", "awaiting_feedback"):
                    iterations_completed = ks_iteration - 1 if ks_iteration > 1 else 0
                else:
                    iterations_completed = ks_iteration
            except (
                OSError,
                json.JSONDecodeError,
                KeyError,
                ValueError,
            ) as e:
                logger.warning("Failed to load KS for job %s: %s", job_model.id, e)

        return JobInfo.from_db_model(job_model, iterations_completed, findings_count)

    def _load_job_info(self, job_id: str) -> Optional[JobInfo]:
        """
        Load job info from database, falling back to config.json if needed.

        This maintains backward compatibility with existing file-based jobs.
        """
        # Try loading from database first
        try:
            job_model = _run_async(_db_get_job(job_id))
            if job_model:
                return self._db_model_to_job_info(job_model)
        except Exception as e:
            logger.warning("Failed to load job %s from database: %s", job_id, e)

        # Fallback to config.json for legacy jobs
        return self._load_job_info_from_file(job_id)

    def _load_job_info_from_file(self, job_id: str) -> Optional[JobInfo]:
        """Load job info from config.json (legacy fallback)."""
        config_path = self.jobs_dir / job_id / "config.json"

        if not config_path.exists():
            return None

        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            # Get progress from config, then override with knowledge_state.json if available
            iterations_completed = config.get("iterations_completed", 0)
            findings_count = config.get("findings_count", 0)

            # Load progress from knowledge_state.json for all jobs
            ks_path = self.jobs_dir / job_id / "knowledge_state.json"
            if ks_path.exists():
                try:
                    with open(ks_path, encoding="utf-8") as f:
                        ks = json.load(f)
                    # Always get findings count from KS (more accurate than config)
                    findings_count = len(ks.get("findings", []))
                    ks_iteration = ks.get("iteration", 1)

                    # For running jobs, iteration is the current (in-progress) iteration
                    # so completed = iteration - 1
                    # For completed/failed/cancelled jobs, iteration is the final count
                    if config["status"] in ("running", "awaiting_feedback"):
                        iterations_completed = ks_iteration - 1 if ks_iteration > 1 else 0
                    else:
                        # Completed/failed/cancelled: use the iteration value directly
                        iterations_completed = ks_iteration
                except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(
                        "Failed to load KS for job %s: %s",
                        job_id,
                        e,
                    )

            return JobInfo(
                job_id=config["job_id"],
                research_question=config["research_question"],
                status=JobStatus(config["status"]),
                created_at=config["created_at"],
                started_at=config.get("started_at"),
                completed_at=config.get("completed_at"),
                failed_at=config.get("failed_at"),
                max_iterations=config["max_iterations"],
                iterations_completed=iterations_completed,
                findings_count=findings_count,
                error=config.get("error"),
                cancellation_reason=config.get("cancellation_reason"),
                use_skills=config.get("use_skills", True),
                investigation_mode=config.get("investigation_mode", "autonomous"),
                owner_id=config.get("owner_id"),
                short_title=config.get("short_title"),
            )

        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to load job info for %s: %s", job_id, e)
            return None

    def _update_job_status_file(
        self,
        job_id: str,
        status: JobStatus,
        cancellation_reason: Optional[str] = None,
    ) -> None:
        """Update job status in config.json (for backward compatibility)."""
        config_path = self.jobs_dir / job_id / "config.json"

        if not config_path.exists():
            return

        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            config["status"] = status.value

            # Update timestamps
            if status == JobStatus.RUNNING and "started_at" not in config:
                config["started_at"] = datetime.now(timezone.utc).isoformat()
            elif status == JobStatus.COMPLETED and "completed_at" not in config:
                config["completed_at"] = datetime.now(timezone.utc).isoformat()
            elif status == JobStatus.FAILED and "failed_at" not in config:
                config["failed_at"] = datetime.now(timezone.utc).isoformat()
            elif status == JobStatus.CANCELLED:
                config["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                if cancellation_reason:
                    config["cancellation_reason"] = cancellation_reason

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to update job status file for %s: %s", job_id, e)

    def _update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        error_message: Optional[str] = None,
        cancellation_reason: Optional[str] = None,
    ) -> None:
        """Update job status in database and config.json."""
        # Get job to find owner
        job_info = self._load_job_info(job_id)
        owner_id = UUID(job_info.owner_id) if job_info and job_info.owner_id else None

        # Update database and get ntfy settings
        ntfy_result: Optional[JobStatusUpdateResult] = None
        try:
            ntfy_result = _run_async(
                _db_update_job_status(job_id, status, error_message, owner_id, cancellation_reason)
            )
        except Exception as e:
            logger.error("Failed to update job status in database: %s", e)

        # Update file for backward compatibility
        self._update_job_status_file(job_id, status, cancellation_reason)

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


def main():
    """CLI entry point for job manager."""
    parser = argparse.ArgumentParser(description="SHANDY Job Manager")
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

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

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
        job = manager.get_job(args.job_id)
        if job is None:
            print(f"Job {args.job_id} not found")
        else:
            print(json.dumps(job.to_dict(), indent=2))

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

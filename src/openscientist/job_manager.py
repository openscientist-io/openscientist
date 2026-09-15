"""
Job manager for OpenScientist discovery jobs.

Handles job lifecycle, status tracking, and cleanup.
"""

import concurrent.futures
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
from openscientist.job.types import JobInfo, JobStatus, JobStatusUpdateResult, RunMode
from openscientist.knowledge_state import KnowledgeState
from openscientist.ntfy import notify_job_status_change
from openscientist.orchestrator import create_job
from openscientist.providers import get_provider
from openscientist.version import get_version_string

logger = logging.getLogger(__name__)

# Statuses a job thread is still alive for: the poll loop in
# _run_job_in_container only exits on completed/failed/cancelled, so a job
# awaiting feedback or generating its report still has a live thread tracked
# in _running_jobs.
_NON_TERMINAL_STATUSES = frozenset(
    {
        JobStatus.RUNNING,
        JobStatus.QUEUED,
        JobStatus.AWAITING_FEEDBACK,
        JobStatus.GENERATING_REPORT,
    }
)


def _effective_model(settings: Any) -> str | None:
    """Resolve the model the active provider will actually use, for recording
    on the job so the UI can show a model badge.

    ``OPENSCIENTIST_MODEL`` (and the anthropic default) are checked first, but
    codex providers may carry their model in provider-specific config (for example
    the Azure deployment) or in a provider default, so when those are unset we ask
    the provider itself. Returns None when nothing resolves (for example codex
    on the account default), leaving the job with a provider badge only.
    """
    model = settings.provider.model or settings.provider.anthropic_default_sonnet_model
    if model:
        return str(model)
    try:
        return get_provider().effective_model_name()
    except Exception as exc:  # provider misconfigured, unavailable, etc.
        logger.debug("Could not resolve provider model for job: %s", exc)
    return None


# Database helper functions for async operations


async def _apply_rls_context(session: AsyncSession, user_id: UUID | None) -> None:
    """Apply user-scoped RLS context when a user id is provided."""
    if user_id is None:
        return
    await session.execute(text("SET ROLE openscientist_app"))
    await set_current_user(session, user_id)


def _derive_progress_from_db(status: str, current_iteration: int) -> int:
    """Derive iterations_completed from Job model columns (no KS load needed)."""
    if status in ("running", "awaiting_feedback"):
        return current_iteration - 1 if current_iteration > 1 else 0
    return current_iteration


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
    short_title: str | None = None,
    description: str | None = None,
    pdb_code: str | None = None,
    space_group: str | None = None,
    llm_provider: str | None = None,
    llm_config: dict[str, Any] | None = None,
) -> JobModel:
    """Create a job in the database (thread-safe for worker threads).

    Args:
        job_id: UUID string for the job (used as primary key)
        research_question: The full research question; drives the agent prompt
        max_iterations: Maximum iterations allowed
        use_hypotheses: Whether hypothesis tracking tools are enabled for this job
        investigation_mode: Investigation mode ('autonomous' or 'coinvestigate')
        owner_id: UUID of the job owner (optional)
        short_title: Optional short display label (truncated to 100 chars)
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
            research_question=research_question,
            short_title=short_title[:100] if short_title else None,
            description=description,
            use_hypotheses=use_hypotheses,
            investigation_mode=investigation_mode,
            status=JobStatus.PENDING.value,
            max_iterations=max_iterations,
            current_iteration=0,
            pdb_code=pdb_code,
            space_group=space_group,
            llm_provider=llm_provider,
            llm_config=llm_config,
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

            result.owner_id = str(job.owner_id) if job.owner_id else None
            result.job_title = job.short_title or job.research_question
            result.current_iteration = job.current_iteration

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


async def _db_get_job_statuses(job_ids: list[str]) -> dict[str, str]:
    """Batch-fetch job statuses by ID."""
    async with AsyncSessionLocal(thread_safe=True) as session:
        stmt = select(JobModel.id, JobModel.status).where(
            JobModel.id.in_([UUID(jid) for jid in job_ids])
        )
        result = await session.execute(stmt)
        return {str(row.id): row.status for row in result}


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
        self._shutting_down = False

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
            if job_info.status in _NON_TERMINAL_STATUSES:
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
        short_title: str | None,
        description: str | None,
        pdb_code: str | None,
        space_group: str | None,
        llm_provider: str | None = None,
        llm_config: dict[str, Any] | None = None,
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
                    short_title=short_title,
                    description=description,
                    pdb_code=pdb_code,
                    space_group=space_group,
                    llm_provider=llm_provider,
                    llm_config=llm_config,
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
        short_title: str | None = None,
        description: str | None = None,
        pdb_code: str | None = None,
        space_group: str | None = None,
    ) -> JobInfo:
        """
        Create a new discovery job.

        Args:
            job_id: Unique job identifier
            research_question: The full research question; drives the agent prompt
            data_files: List of data file paths (can be empty for literature-only jobs)
            max_iterations: Maximum iterations
            use_hypotheses: Whether to enable hypothesis tracking tools
            auto_start: Whether to start job immediately
            investigation_mode: "autonomous" (default) or "coinvestigate"
            owner_id: UUID of the job owner (optional, for orphaned jobs)
            short_title: Optional short display label (truncated to 100 chars)
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

        # Capture provider & model from current settings
        from openscientist.settings import get_settings

        settings = get_settings()
        llm_provider = settings.provider.provider_id.lower()
        model = _effective_model(settings)
        llm_config = {"model": model} if model else None

        # Create job in database
        logger.info("Creating job %s in database", job_id)
        owner_uuid = self._create_db_job_record(
            job_id=job_id,
            research_question=research_question,
            max_iterations=max_iterations,
            use_hypotheses=use_hypotheses,
            investigation_mode=investigation_mode,
            owner_id=owner_id,
            short_title=short_title,
            description=description,
            pdb_code=pdb_code,
            space_group=space_group,
            llm_provider=llm_provider,
            llm_config=llm_config,
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
            if self._shutting_down:
                raise ValueError("Job manager is shutting down; cannot start new jobs")

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

    def regenerate_report(self, job_id: str) -> None:
        """Re-run only the report-generation phase for a completed job.

        Backs the admin "Regenerate report" action. Launches the agent
        container in report-only mode (no discovery iterations: the findings in
        the persisted KnowledgeState are reused). The job must exist, be
        completed, and not currently running, and a concurrency slot must be
        free. Unlike ``start_job`` this does NOT queue: report regeneration is a
        one-off admin action, so it fails loudly rather than waiting.

        Raises:
            ValueError: If the job is missing, not completed, already running,
                or the max concurrent limit is reached.
        """
        with self._lock:
            if self._shutting_down:
                raise ValueError("Job manager is shutting down; cannot start new jobs")
            job_info = self.get_job(job_id)
            if job_info is None:
                raise ValueError(f"Job {job_id} not found")
            if job_info.status != JobStatus.COMPLETED:
                raise ValueError(f"Job {job_id} is not completed (status: {job_info.status.value})")
            if job_id in self._running_jobs:
                raise ValueError(f"Job {job_id} is already running")
            if self._get_active_job_count() >= self.max_concurrent:
                raise ValueError("Cannot regenerate report: maximum concurrent jobs reached")

            logger.info("Regenerating report for job %s", job_id)
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id,),
                kwargs={"run_mode": RunMode.REPORT_ONLY},
                daemon=True,
            )
            self._running_jobs[job_id] = thread
            thread.start()

    def _run_job(self, job_id: str, run_mode: RunMode = RunMode.DISCOVERY) -> None:
        """Run a job (internal, called by thread)."""
        self._run_job_in_container(job_id, run_mode=run_mode)

    def _run_job_in_container(self, job_id: str, run_mode: RunMode = RunMode.DISCOVERY) -> None:
        """Launch an agent container for the job and block until it reaches a terminal status.

        ``run_mode`` is RunMode.DISCOVERY for the full loop or RunMode.REPORT_ONLY
        to re-run just the report-generation phase against the persisted findings.
        """
        from openscientist.job_container import JobContainerRunner

        poll_interval = 5
        terminal_statuses = {"completed", "failed", "cancelled"}

        job_dir = self.jobs_dir / job_id
        runner = JobContainerRunner()

        try:
            self._update_job_status(job_id, JobStatus.RUNNING)
            runner.launch(job_id, job_dir, run_mode=run_mode)
            logger.info("Agent container launched for job %s (mode=%s)", job_id, run_mode)

            # Poll the database until the container's agent writes a terminal status.
            # Also check if the container has exited unexpectedly (crash before DB write).
            from openscientist.settings import get_settings

            timeout_seconds = get_settings().container.agent_timeout
            timeout_hours = timeout_seconds / 3600
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
                        (
                            "Agent container for job %s exited with code %d before "
                            "writing terminal status"
                        ),
                        job_id,
                        exit_code,
                    )
                    container_logs = runner.get_logs(job_id)
                    self._update_job_status(
                        job_id,
                        JobStatus.FAILED,
                        error_message=self._build_container_failure_message(
                            exit_code, container_logs
                        ),
                    )
                    return

            # Hard timeout reached.
            logger.error("Container job %s timed out after %.1f hours", job_id, timeout_hours)
            self._update_job_status(
                job_id,
                JobStatus.FAILED,
                error_message=f"Job timed out after {timeout_hours:.1f} hours",
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

    def _build_container_failure_message(self, exit_code: int, container_logs: str | None) -> str:
        """Compose a diagnostic message for an agent container that exited
        non-zero before writing a terminal status.

        The agent entrypoint catches its own exceptions, logs the traceback to
        stderr, and exits 1 — so without surfacing the container logs here the
        failure is opaque in the UI. Include the log tail plus a hint for the
        most common local-dev misconfiguration.
        """
        parts = [f"Agent container exited with code {exit_code}."]

        hint = self._host_project_dir_hint()
        if hint:
            parts.append(hint)

        if container_logs:
            parts.append(f"Last container log lines:\n{container_logs.strip()}")

        return "\n\n".join(parts)

    def _host_project_dir_hint(self) -> str | None:
        """Return a fix hint when the agent job directory was likely mounted empty.

        When the web app itself runs inside Docker but OPENSCIENTIST_HOST_PROJECT_DIR
        is unset, sibling agent containers bind-mount a non-existent host path, so
        the job directory arrives empty and the agent crashes immediately on
        startup. Detect that specific case and point the operator at the fix.
        """
        try:
            from openscientist.settings import get_settings

            in_container = Path("/.dockerenv").exists()
            host_project_dir = get_settings().container.host_project_dir
        except Exception:  # pragma: no cover - defensive; settings load elsewhere
            return None

        if in_container and not host_project_dir:
            return (
                "Likely cause: the web app is running in Docker but "
                "OPENSCIENTIST_HOST_PROJECT_DIR is not set, so the agent container's "
                "job directory is mounted empty. Set OPENSCIENTIST_HOST_PROJECT_DIR to "
                "the absolute host path of the project (the directory that contains "
                "./jobs) and recreate the web container."
            )
        return None

    def _start_next_queued_job(self) -> None:
        """Start the next queued job if slots available."""
        with self._lock:
            if self._shutting_down:
                return
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

    def shutdown(self, timeout: float = 30.0) -> None:
        """Drain in-flight job threads gracefully before process exit.

        Job threads are daemon threads (see ``start_job``), so on their own
        they would be killed the instant the process exits, leaving
        containers running and job rows stuck at RUNNING forever. This stops
        the active jobs the same way ``cancel_job`` does — mark cancelled in
        the DB, then SIGTERM the container — and waits up to ``timeout``
        total for their worker threads to notice and exit. Also blocks
        ``start_job``/``regenerate_report``/``_start_next_queued_job`` from
        spawning new threads for the remainder of the process lifetime, so a
        job finishing mid-drain can't queue a fresh one behind our back.

        Cancelling and stopping containers happens concurrently across jobs:
        ``JobContainerRunner.stop`` blocks for up to its own 10s timeout per
        container, so doing this one job at a time would let N concurrent
        jobs burn up to N*10s before the join phase below even starts,
        blowing well past ``timeout`` when more than one job is running.
        """
        with self._lock:
            self._shutting_down = True
            job_ids = list(self._running_jobs.keys())
            threads = list(self._running_jobs.values())

        if not threads:
            return

        logger.info("Shutting down: draining %d in-flight job(s)", len(threads))

        from openscientist.job_container import JobContainerRunner

        runner = JobContainerRunner()

        def _cancel_and_stop(job_id: str) -> None:
            job_info = self.get_job(job_id)
            if job_info is not None and job_info.status in _NON_TERMINAL_STATUSES:
                self._update_job_status(
                    job_id,
                    JobStatus.CANCELLED,
                    cancellation_reason="Server shut down while job was running",
                )
                try:
                    runner.stop(job_id)
                except Exception as e:
                    logger.warning(
                        "Failed to stop container for job %s during shutdown: %s", job_id, e
                    )

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(job_ids)) as pool:
            list(pool.map(_cancel_and_stop, job_ids))

        per_thread_timeout = timeout / len(threads)
        stuck_job_ids = []
        for job_id, thread in zip(job_ids, threads, strict=True):
            thread.join(timeout=per_thread_timeout)
            if thread.is_alive():
                stuck_job_ids.append(job_id)

        if stuck_job_ids:
            logger.warning(
                "%d job thread(s) did not exit within %.1fs shutdown timeout: %s",
                len(stuck_job_ids),
                timeout,
                stuck_job_ids,
            )
            with self._lock:
                for job_id in stuck_job_ids:
                    self._running_jobs.pop(job_id, None)
            for job_id in stuck_job_ids:
                try:
                    runner.cleanup(job_id, log_dir=self.jobs_dir / job_id)
                except Exception as e:
                    logger.warning(
                        "Failed to clean up container for job %s during shutdown: %s", job_id, e
                    )

        logger.info("Job manager shutdown complete")

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
        if not self._running_jobs:
            return 0
        job_ids = list(self._running_jobs.keys())
        statuses = _run_async(_db_get_job_statuses(job_ids))
        return sum(1 for s in statuses.values() if s != JobStatus.AWAITING_FEEDBACK.value)

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
            budget_check = provider.evaluate_budget(cost_info)
        except (ValueError, ProviderError) as e:
            logger.warning("Could not fetch cost info: %s", e)
            cost_info = None
            budget_check = None

        return {
            "total_jobs": len(jobs),
            "status_counts": status_counts,
            "cost_info": cost_info.to_dict() if cost_info is not None else None,
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
        ntfy_result: JobStatusUpdateResult | None = None
        try:
            ntfy_result = _run_async(
                _db_update_job_status(job_id, status, error_message, None, cancellation_reason)
            )
        except Exception as e:
            logger.error("Failed to update job status in database: %s", e)

        if not ntfy_result or not ntfy_result.ntfy_enabled or not ntfy_result.owner_id:
            return

        iterations_completed = _derive_progress_from_db(status.value, ntfy_result.current_iteration)
        try:
            _run_async(
                notify_job_status_change(
                    user_id=UUID(ntfy_result.owner_id),
                    job_id=job_id,
                    job_title=ntfy_result.job_title or "",
                    new_status=status.value,
                    error_message=error_message,
                    cancellation_reason=cancellation_reason,
                    iteration=iterations_completed,
                    ntfy_topic=ntfy_result.ntfy_topic,
                )
            )
        except Exception as e:
            logger.warning("Failed to send ntfy notification: %s", e)


def main() -> None:
    """CLI entry point for job manager.

    Compatibility wrapper: the implementation now lives in
    :mod:`openscientist.job.cli`. Imported lazily to avoid a circular import
    (``job.cli`` imports :class:`JobManager` from this module at module load
    time).
    """
    from openscientist.job.cli import main as _cli_main

    _cli_main()


if __name__ == "__main__":
    main()

"""
Job manager for OpenScientist discovery jobs.
"""

import argparse
import asyncio
import json
import logging
import shutil
import threading
import time
from collections.abc import Collection, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from openscientist.admin_settings import (
    AdminSettingsMissingError,
    current_admin_settings,
    current_admin_settings_sync,
)
from openscientist.async_tasks import run_sync, thread_loop
from openscientist.budget import check_budget
from openscientist.container_manager import get_container_manager
from openscientist.database.models.job import Job as JobModel
from openscientist.database.retry import (
    BACKGROUND_BACKOFF_CAP_SECONDS,
    backoff,
    is_connection_error,
    retry_sync,
)
from openscientist.database.session import thread_safe_admin_session
from openscientist.exceptions import BudgetExceededError, ProviderError
from openscientist.job.agent_error import take_agent_error
from openscientist.job.repository import (
    any_queued_job_held,
    backfill_job_end_time,
    create_job_row,
    delete_job,
    get_job,
    get_notification_target,
    list_jobs,
    next_queued_job,
    set_job_status,
    set_queue_reason,
)
from openscientist.job.types import (
    JobInfo,
    JobStatus,
    JobStatusUpdateResult,
    completed_iterations,
)
from openscientist.knowledge_state import KnowledgeState
from openscientist.ntfy import (
    ensure_user_has_topic,
    notify_job_budget_hold,
    notify_job_status_change,
)
from openscientist.orchestrator import create_job
from openscientist.providers import get_provider
from openscientist.settings import ProviderSettings, get_settings
from openscientist.version import get_version_string

logger = logging.getLogger(__name__)


def _effective_model(provider: ProviderSettings) -> str | None:
    """Resolve the model the active provider will actually use, for the job's UI badge.

    Returns None when nothing resolves (for example codex on the account
    default), leaving the job with a provider badge only.
    """
    model = provider.model or provider.anthropic_default_sonnet_model
    if model:
        return str(model)
    try:
        return get_provider().effective_model_name()
    except Exception as exc:  # provider misconfigured, unavailable, etc.
        logger.debug("Could not resolve provider model for job: %s", exc)
    return None


async def _load_progress(
    job_id: str,
    status: str,
    default_iterations: int,
    default_findings: int,
) -> tuple[int, int]:
    """Load progress as the admin login, since job lists and polls read it for no user."""
    try:
        async with thread_safe_admin_session() as session:
            ks = (await KnowledgeState.load_from_database(job_id, session)).data
        findings_count = len(ks.get("findings", []))
        ks_iteration = int(ks.get("iteration", 1))
        return completed_iterations(status, ks_iteration), findings_count
    except Exception as e:
        logger.warning("Failed to load KS for job %s: %s", job_id, e)
        return default_iterations, default_findings


def _load_progress_from_knowledge_state(
    job_id: str,
    status: str,
    default_iterations: int,
    default_findings: int,
) -> tuple[int, int]:
    """:func:`_load_progress` for worker threads."""
    return run_sync(_load_progress(job_id, status, default_iterations, default_findings))


class JobManager:
    """Manages OpenScientist discovery jobs."""

    #: A job that has not ended, so it may still fail or be cancelled.
    ACTIVE_STATUSES = frozenset(
        {
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.AWAITING_FEEDBACK,
            JobStatus.GENERATING_REPORT,
        }
    )
    #: A job waiting for its worker thread.
    WAITING_STATUSES = frozenset({JobStatus.PENDING, JobStatus.QUEUED})
    #: A job whose container has started.
    STARTED_STATUSES = frozenset(
        {JobStatus.RUNNING, JobStatus.AWAITING_FEEDBACK, JobStatus.GENERATING_REPORT}
    )

    def __init__(self, jobs_dir: Path = Path("jobs")):
        """Initialize job manager. Building one changes no job.

        Only the web's startup calls :meth:`recover_after_restart`. Each slot
        check and queue pick reads the admin settings row, so an admin edit
        applies at the next check.
        """
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        self._running_jobs: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        # A pick that lost the database tries again on its own, since nothing
        # else may trigger one on an idle server.
        self._repick: threading.Timer | None = None
        self._repick_delays: Iterator[float] | None = None

        logger.info("JobManager initialized: %s", self.jobs_dir)

    def recover_after_restart(self) -> None:
        """End the jobs whose container the previous web process lost, and start
        the jobs still waiting for a slot. Waits out a lost database connection."""
        jobs = retry_sync(self._list_operational_jobs, what="listing jobs to recover")
        for job_info in jobs:
            try:
                self._recover_job(job_info)
            except Exception:
                logger.exception("Could not recover job %s", job_info.job_id)
        while self._start_next_queued_job():
            pass

    def _recover_job(self, job_info: JobInfo) -> None:
        job_id = job_info.job_id
        lost_container = self.STARTED_STATUSES
        if job_info.status == JobStatus.PENDING:
            self._update_job_status(
                job_id, JobStatus.QUEUED, allowed_from={JobStatus.PENDING}, notify=False, retry=True
            )
            return
        if job_info.status not in lost_container:
            return
        logger.warning(
            "Cancelling job %s, whose container the restart lost (was %s)",
            job_id,
            job_info.status.value,
        )
        self._update_job_status(
            job_id,
            JobStatus.CANCELLED,
            allowed_from=lost_container,
            cancellation_reason="Server restarted while job was running",
            notify=False,
            retry=True,
        )
        try:
            from openscientist.job_container import JobContainerRunner

            JobContainerRunner().cleanup(job_id)
        except Exception as e:
            logger.warning("Failed to cleanup container for stale job %s: %s", job_id, e)

    async def _create_db_job_record(
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
            await create_job_row(
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
        except Exception as e:
            logger.error("Failed to create job in database: %s", e)
            raise ValueError(f"Failed to create job in database: {e}") from e
        return owner_uuid

    def _rollback_failed_job_creation(self, job_id: str, owner_uuid: UUID | None) -> None:
        try:
            run_sync(delete_job(job_id, owner_uuid))
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
            # Compensating cleanup for a partially-created job, so the DB and disk never disagree.
            self._rollback_failed_job_creation(job_id, owner_uuid)
            raise ValueError(f"Failed to initialize job files: {e}") from e

    async def create_job(
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
        """Create a new discovery job.

        data_files may be empty for literature-only jobs.
        """
        if await self.get_job(job_id) is not None:
            raise ValueError(f"Job {job_id} already exists")

        settings = get_settings()
        llm_provider = settings.provider.provider_id.lower()
        model = _effective_model(settings.provider)
        llm_config = {"model": model} if model else None

        verdict = await check_budget(provider_id=llm_provider, model=model, include_unpriced=True)
        if not verdict.ok:
            raise BudgetExceededError(verdict.message or "A budget limit is reached")
        logger.info("Creating job %s in database", job_id)
        owner_uuid = await self._create_db_job_record(
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

        # Git init, file copies and the first knowledge-state save block, so
        # they run on a worker thread.
        logger.info("Creating job %s filesystem structure", job_id)
        await asyncio.to_thread(
            self._create_job_files,
            job_id=job_id,
            research_question=research_question,
            data_files=data_files,
            max_iterations=max_iterations,
            owner_id=owner_id,
            owner_uuid=owner_uuid,
        )

        job_info = await self.get_job(job_id)
        if job_info is None:
            raise ValueError(f"Failed to load newly created job {job_id}")

        if auto_start:
            await asyncio.to_thread(self.start_job, job_id)

        return job_info

    def start_job(self, job_id: str) -> None:
        """Start a job on a worker thread, or queue it when no slot is free. It
        reads and writes the database synchronously, so callers on an event
        loop run it through ``asyncio.to_thread``."""
        with self._lock:
            job_info = self._load_job_info(job_id)
            if job_info is None:
                raise ValueError(f"Job {job_id} not found")

            if job_id in self._running_jobs:
                raise ValueError(f"Job {job_id} is already running")

            if job_info.status not in self.WAITING_STATUSES:
                raise ValueError(
                    f"Job {job_id} cannot be started from status {job_info.status.value}"
                )

            if self._get_active_job_count() >= current_admin_settings_sync().max_concurrent_jobs:
                if not self._update_job_status(
                    job_id, JobStatus.QUEUED, allowed_from=self.WAITING_STATUSES, retry=False
                ):
                    raise ValueError(f"Job {job_id} cannot be started: it stopped waiting")
                logger.info("Job %s queued (max concurrent reached)", job_id)
                return

            logger.info("Starting job %s", job_id)
            thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
            self._running_jobs[job_id] = thread
            thread.start()

    async def regenerate_report(self, job_id: str) -> None:
        """Re-run only the report-generation phase for a completed job.

        Launches the agent container in report-only mode, reusing the findings
        in the persisted KnowledgeState. Unlike ``start_job`` this does not
        queue, because a one-off admin action should fail loudly rather than wait.
        """
        job_info = await self.get_job(job_id)
        with self._lock:
            if job_info is None:
                raise ValueError(f"Job {job_id} not found")
            if job_info.status != JobStatus.COMPLETED:
                raise ValueError(f"Job {job_id} is not completed (status: {job_info.status.value})")
            if job_id in self._running_jobs:
                raise ValueError(f"Job {job_id} is already running")
            if self._get_active_job_count() >= (await current_admin_settings()).max_concurrent_jobs:
                raise ValueError("Cannot regenerate report: maximum concurrent jobs reached")

            verdict = await check_budget(
                job_id=UUID(job_id),
                provider_id=job_info.llm_provider or "",
                model=job_info.llm_model or _effective_model(get_settings().provider),
                include_unpriced=True,
            )
            if not verdict.ok:
                raise BudgetExceededError(verdict.message or "A budget limit is reached")

            logger.info("Regenerating report for job %s", job_id)
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id,),
                kwargs={"run_mode": "report_only"},
                daemon=True,
            )
            self._running_jobs[job_id] = thread
            thread.start()

    def _run_job(self, job_id: str, run_mode: str = "discovery") -> None:
        """Run a job on its own thread, whose database calls share one loop and
        its pooled connections for the whole run."""
        with thread_loop():
            self._run_job_in_container(job_id, run_mode=run_mode)

    def _run_job_in_container(self, job_id: str, run_mode: str = "discovery") -> None:
        """Launch an agent container for the job and block until it reaches a terminal status.

        ``run_mode`` is "discovery" for the full loop or "report_only" to
        re-run just the report-generation phase against the persisted findings.
        """
        from openscientist.job_container import JobContainerRunner

        poll_interval = 5
        terminal_statuses = {"completed", "failed", "cancelled"}

        job_dir = self.jobs_dir / job_id
        runner = JobContainerRunner()
        running_from = {JobStatus.COMPLETED} if run_mode == "report_only" else self.WAITING_STATUSES
        launched = False

        try:
            if not self._update_job_status(
                job_id, JobStatus.RUNNING, allowed_from=running_from, retry=True
            ):
                logger.info("Job %s stopped waiting to start, so it is not launched", job_id)
                return
            launched = True
            take_agent_error(job_dir)  # an earlier run's, which this run must not report
            runner.launch(job_id, job_dir, run_mode=run_mode)
            logger.info("Agent container launched for job %s (mode=%s)", job_id, run_mode)

            # Poll until the agent writes a terminal status or the container crashed without one.
            timeout_seconds = current_admin_settings_sync().agent_timeout_seconds
            timeout_hours = timeout_seconds / 3600
            elapsed = 0
            last_status = JobStatus.RUNNING
            while elapsed < timeout_seconds:
                time.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    job_info = self._load_job_info(job_id)
                    if job_info and job_info.status != last_status:
                        last_status = job_info.status
                        self._notify_container_transition(job_info)
                    if job_info and job_info.status.value in terminal_statuses:
                        # The container's ending write carries no time, so this
                        # first observation stamps it before the job leaves the slot.
                        run_sync(backfill_job_end_time(job_id))
                        logger.info(
                            "Container job %s reached terminal status: %s",
                            job_id,
                            job_info.status.value,
                        )
                        return
                except Exception as poll_err:
                    if not is_connection_error(poll_err):
                        raise
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
                    exited = f"Agent container exited with code {exit_code}"
                    agent_error = take_agent_error(job_dir)
                    self._update_job_status(
                        job_id,
                        JobStatus.FAILED,
                        allowed_from=self.STARTED_STATUSES,
                        error_message=f"{exited}: {agent_error}" if agent_error else exited,
                        retry=True,
                    )
                    return

            logger.error("Container job %s timed out after %.1f hours", job_id, timeout_hours)
            self._update_job_status(
                job_id,
                JobStatus.FAILED,
                allowed_from=self.STARTED_STATUSES,
                error_message=f"Job timed out after {timeout_hours:.1f} hours",
                retry=True,
            )

        except Exception as e:
            logger.error(
                "Container job %s failed [%s]: %s", job_id, get_version_string(), e, exc_info=True
            )
            self._update_job_status(
                job_id,
                JobStatus.FAILED,
                allowed_from=self.ACTIVE_STATUSES,
                error_message=str(e),
                retry=True,
            )

        finally:
            if launched:
                runner.cleanup(job_id, log_dir=job_dir)
            with self._lock:
                self._running_jobs.pop(job_id, None)
            self._start_next_queued_job()

    def _start_next_queued_job(self) -> bool:
        """Start the queued job the balancing order picks, if a slot is free.

        A project limit keeps the job queued with the reason it waits on, and
        an unpriced model ends it failed before the container starts, after
        which the pick goes on to the next queued job.
        """
        with self._lock:
            try:
                admin = current_admin_settings_sync()
            except Exception as e:
                # Startup recovery can pick before the web has seeded the row.
                if is_connection_error(e) or isinstance(e, AdminSettingsMissingError):
                    self._schedule_repick(e)
                else:
                    logger.exception("Failed to read the admin settings for the next pick")
                return False

            while True:
                if self._get_active_job_count() >= admin.max_concurrent_jobs:
                    return False

                try:
                    job_id = run_sync(
                        next_queued_job(
                            timedelta(hours=admin.scheduler_window_hours),
                            set(self._running_jobs),
                        )
                    )
                    if job_id is None:
                        self._repick_delays = None
                        return False
                    job = run_sync(get_job(job_id))
                    if job is None:
                        # Deleted between the pick and this read; the next
                        # pick takes the next queued job.
                        self._repick_delays = None
                        return False
                    verdict = run_sync(
                        check_budget(
                            job_id=UUID(job_id),
                            provider_id=job.llm_provider,
                            model=(job.llm_config or {}).get("model")
                            or _effective_model(get_settings().provider),
                            include_unpriced=True,
                        )
                    )
                except Exception as e:
                    if is_connection_error(e):
                        self._schedule_repick(e)
                    else:
                        logger.exception("Failed to pick the next queued job")
                    return False
                self._repick_delays = None

                if verdict.limit in ("project_total", "project_24h"):
                    reason = verdict.message or "A project budget limit is reached"
                    first_hold = run_sync(set_queue_reason(job_id, reason)) is None
                    logger.info("Job %s held by a budget limit: %s", job_id, reason)
                    if first_hold:
                        run_sync(_notify_budget_hold(job_id, reason))
                    return False

                if verdict.limit == "unpriced_model":
                    logger.info("Job %s not starting: %s", job_id, verdict.message)
                    self._update_job_status(
                        job_id,
                        JobStatus.FAILED,
                        allowed_from={JobStatus.QUEUED},
                        error_message=verdict.message or "No price is known for the job's model",
                        retry=True,
                    )
                    # The failure frees the queue for the rest.
                    continue

                if run_sync(set_queue_reason(job_id, None)) is not None:
                    logger.info("Job %s no longer held by a budget limit", job_id)

                logger.info("Starting queued job %s", job_id)
                thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
                self._running_jobs[job_id] = thread
                thread.start()
                return True

    def _schedule_repick(self, error: Exception) -> None:
        """Pick again after the next backoff delay, unless a retry is already pending.
        Called with ``self._lock`` held."""
        if self._repick is not None:
            return
        if self._repick_delays is None:
            self._repick_delays = backoff(cap=BACKGROUND_BACKOFF_CAP_SECONDS)
        delay = next(self._repick_delays)
        logger.error(
            "Could not pick the next queued job, picking again in %.0f s: %s", delay, error
        )
        self._repick = threading.Timer(delay, self._repick_now)
        self._repick.daemon = True
        self._repick.start()

    def _repick_now(self) -> None:
        with self._lock:
            self._repick = None
        while self._start_next_queued_job():
            pass

    async def cancel_job(self, job_id: str) -> None:
        """Cancel a job that has not ended, stopping its container if it has one."""
        if await get_job(job_id) is None:
            raise ValueError(f"Job {job_id} not found")

        cancellation_reason = "Cancelled by user"
        target = await set_job_status(
            job_id,
            JobStatus.CANCELLED,
            allowed_from=self.ACTIVE_STATUSES,
            cancellation_reason=cancellation_reason,
        )
        if not target.applied:
            raise ValueError(f"Job {job_id} has already ended")
        await set_queue_reason(job_id, None)
        await _status_notification(
            job_id, JobStatus.CANCELLED, target, cancellation_reason=cancellation_reason
        )

        # The worker thread stays tracked until it exits, so slots stay counted.
        # A thread that has not launched yet sees the cancel and does not launch.
        with self._lock:
            has_worker = job_id in self._running_jobs

        # Stop the container now rather than when the worker's poll sees the cancel.
        # docker stop waits for the container, so it runs on a worker thread.
        if has_worker:
            await asyncio.to_thread(self._stop_container, job_id)

        logger.info("Job %s cancelled", job_id)
        await asyncio.to_thread(self._start_next_queued_job)

    @staticmethod
    def _stop_container(job_id: str) -> None:
        try:
            from openscientist.job_container import JobContainerRunner

            JobContainerRunner().stop(job_id)
        except Exception as e:
            logger.warning("Failed to stop container for cancelled job %s: %s", job_id, e)

    async def get_job(self, job_id: str) -> JobInfo | None:
        """Get job information."""
        job_model = await get_job(job_id)
        return None if job_model is None else await self._job_info(job_model)

    async def list_jobs(
        self,
        status: JobStatus | None = None,
        limit: int | None = None,
    ) -> list[JobInfo]:
        """List all jobs from the database, without user filtering.

        For user-facing queries, use a database session with RLS instead.
        """
        db_jobs = await list_jobs(status=status, limit=limit)
        return [await self._job_info(m) for m in db_jobs]

    async def delete_job(self, job_id: str) -> None:
        """Delete a job and its files."""
        job_model = await get_job(job_id)
        if job_model is None:
            raise ValueError(f"Job {job_id} not found")

        if job_model.status in (JobStatus.RUNNING.value, JobStatus.GENERATING_REPORT.value):
            raise ValueError(f"Cannot delete running job {job_id}")

        try:
            await delete_job(job_id, job_model.owner_id)
        except Exception as e:
            logger.error("Failed to delete job from database: %s", e)
            raise ValueError(f"Failed to delete job {job_id} from database: {e}") from e

        # Docker calls and the directory removal block, so they run on a worker thread.
        await asyncio.to_thread(self._remove_job_artifacts, job_id)

    def _remove_job_artifacts(self, job_id: str) -> None:
        try:
            container_manager = get_container_manager()
            if container_manager.is_available():
                removed = container_manager.cleanup_job_containers(job_id)
                if removed > 0:
                    logger.info("Removed %d executor container(s) for job %s", removed, job_id)
        except Exception as e:
            logger.warning("Failed to cleanup containers for job %s: %s", job_id, e)

        job_dir = self.jobs_dir / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)
            logger.info("Deleted job %s", job_id)

    def cleanup_old_jobs(self, days: int = 7, keep_completed: bool = True) -> int:
        """Clean up old jobs."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        deleted = 0

        for job_info in self._list_operational_jobs():
            job_id = job_info.job_id

            if job_info.status == JobStatus.RUNNING:
                continue

            if keep_completed and job_info.status == JobStatus.COMPLETED:
                continue

            created_at = datetime.fromisoformat(job_info.created_at)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if created_at < cutoff:
                try:
                    run_sync(self.delete_job(job_id))
                    deleted += 1
                except (OSError, ValueError) as e:
                    logger.error("Failed to delete job %s: %s", job_id, e)

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
        """Count tracked jobs, including those paused for feedback, which keep their container."""
        return len(self._running_jobs)

    async def get_job_summary(self) -> dict[str, Any]:
        """Get a summary of all jobs, without user filtering."""
        jobs = await self.list_jobs()

        status_counts = {}
        for status in JobStatus:
            status_counts[status.value] = sum(1 for j in jobs if j.status == status)

        try:
            provider = get_provider()
            cost_info = await asyncio.to_thread(provider.get_cost_info, lookback_hours=24)
        except (ValueError, ProviderError) as e:
            logger.warning("Could not fetch cost info: %s", e)
            cost_info = None

        return {
            "total_jobs": len(jobs),
            "status_counts": status_counts,
            "cost_info": cost_info,
        }

    def _list_operational_jobs(
        self,
        status: JobStatus | None = None,
        limit: int | None = None,
    ) -> list[JobInfo]:
        """List jobs for operational workflows."""
        db_jobs = run_sync(list_jobs(status=status, limit=limit))
        return [self._db_model_to_job_info(job) for job in db_jobs]

    def _db_model_to_job_info(self, job_model: JobModel) -> JobInfo:
        """Convert a database JobModel to JobInfo with real-time progress from KS."""
        job_id = str(job_model.id)
        iterations_completed, findings_count = _load_progress_from_knowledge_state(
            job_id=job_id,
            status=job_model.status,
            default_iterations=job_model.current_iteration,
            default_findings=0,
        )

        return JobInfo.from_db_model(job_model, iterations_completed, findings_count)

    async def _job_info(self, job_model: JobModel) -> JobInfo:
        """:meth:`_db_model_to_job_info` for callers on an event loop."""
        iterations_completed, findings_count = await _load_progress(
            str(job_model.id), job_model.status, job_model.current_iteration, 0
        )
        return JobInfo.from_db_model(job_model, iterations_completed, findings_count)

    def _load_job_info(self, job_id: str) -> JobInfo | None:
        """
        Load job info from database.
        """
        job_model = run_sync(get_job(job_id))
        return None if job_model is None else self._db_model_to_job_info(job_model)

    def _update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        allowed_from: Collection[JobStatus],
        error_message: str | None = None,
        cancellation_reason: str | None = None,
        notify: bool = True,
        retry: bool,
    ) -> bool:
        """Move the job to ``status`` if it is in one of ``allowed_from``, and tell
        the owner when ``notify`` is set. Returns whether the write applied.

        With ``retry``, a lost database connection is retried until the write
        goes through. Any other database error is raised."""

        def write() -> JobStatusUpdateResult:
            return run_sync(
                set_job_status(
                    job_id,
                    status,
                    allowed_from=allowed_from,
                    error_message=error_message,
                    cancellation_reason=cancellation_reason,
                )
            )

        target = (
            retry_sync(write, what=f"moving job {job_id} to {status.value}") if retry else write()
        )
        if not target.applied:
            return False
        if notify:
            self._send_status_notification(
                job_id,
                status,
                target,
                error_message=error_message,
                cancellation_reason=cancellation_reason,
            )
        return True

    #: Statuses the agent container writes that the owner is told about. The web
    #: process notifies the ones it writes itself in ``_update_job_status``.
    _CONTAINER_NOTIFIED_STATUSES: frozenset[JobStatus] = frozenset(
        {JobStatus.AWAITING_FEEDBACK, JobStatus.COMPLETED, JobStatus.FAILED}
    )

    def _notify_container_transition(self, job_info: JobInfo) -> None:
        """Notify a status change the poll saw the agent container write."""
        if job_info.status not in self._CONTAINER_NOTIFIED_STATUSES:
            return
        try:
            target = run_sync(get_notification_target(job_info.job_id))
        except Exception as e:
            logger.warning("Failed to load notification target for %s: %s", job_info.job_id, e)
            return
        self._send_status_notification(
            job_info.job_id,
            job_info.status,
            target,
            error_message=job_info.error,
            stop_note=job_info.stop_note if job_info.status is JobStatus.COMPLETED else None,
        )

    @staticmethod
    def _send_status_notification(
        job_id: str,
        status: JobStatus,
        target: JobStatusUpdateResult | None,
        *,
        error_message: str | None = None,
        cancellation_reason: str | None = None,
        stop_note: str | None = None,
    ) -> None:
        """:func:`_status_notification` for worker threads."""
        run_sync(
            _status_notification(
                job_id,
                status,
                target,
                error_message=error_message,
                cancellation_reason=cancellation_reason,
                stop_note=stop_note,
            )
        )


async def _status_notification(
    job_id: str,
    status: JobStatus,
    target: JobStatusUpdateResult | None,
    *,
    error_message: str | None = None,
    cancellation_reason: str | None = None,
    stop_note: str | None = None,
) -> None:
    """Tell the job's owner about ``status``, if they turned notifications on."""
    if not target or not target.ntfy_enabled or not target.owner_id:
        return
    iterations_completed = completed_iterations(status.value, target.current_iteration)
    try:
        await notify_job_status_change(
            user_id=UUID(target.owner_id),
            job_id=job_id,
            job_title=target.job_title or "",
            new_status=status.value,
            error_message=error_message,
            cancellation_reason=cancellation_reason,
            iteration=iterations_completed,
            stop_note=stop_note,
            ntfy_topic=target.ntfy_topic,
        )
    except Exception as e:
        logger.warning("Failed to send ntfy notification: %s", e)


async def _notify_budget_hold(job_id: str, reason: str) -> None:
    """Tell the job's owner once that their queued job waits on a budget limit."""
    try:
        target = await get_notification_target(job_id)
    except Exception as e:
        logger.warning("Failed to load notification target for %s: %s", job_id, e)
        return
    if not target or not target.ntfy_enabled or not target.owner_id:
        return
    topic = target.ntfy_topic
    if not topic:
        topic = await ensure_user_has_topic(UUID(target.owner_id))
    if not topic:
        return
    try:
        await notify_job_budget_hold(
            topic=topic,
            job_id=job_id,
            title=target.job_title or "",
            reason=reason,
            base_url=get_settings().base_url,
        )
    except Exception as e:
        logger.warning("Failed to send ntfy notification: %s", e)


#: How often the queue is re-picked while a project limit holds jobs back.
BUDGET_QUEUE_RECHECK_INTERVAL_SECONDS = 60.0


async def run_budget_queue_recheck(
    manager: JobManager, interval: float = BUDGET_QUEUE_RECHECK_INTERVAL_SECONDS
) -> None:
    """Pick again every ``interval`` seconds while some queued job waits on a
    budget limit.

    The queue is picked on a slot freeing alone, so without this a job held by
    a 24-hour limit would wait for a slot free that may never come, past the
    window rolling on."""
    while True:
        await asyncio.sleep(interval)
        try:
            if await any_queued_job_held():
                await asyncio.to_thread(manager._start_next_queued_job)
        except Exception as e:
            logger.warning("Budget queue re-check failed: %s", e)


def main() -> None:
    """CLI entry point for job manager."""
    parser = argparse.ArgumentParser(description="OpenScientist Job Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List jobs")
    list_parser.add_argument("--status", help="Filter by status")
    list_parser.add_argument("--limit", type=int, help="Limit number of jobs")

    get_parser = subparsers.add_parser("get", help="Get job info")
    get_parser.add_argument("job_id", help="Job ID")

    delete_parser = subparsers.add_parser("delete", help="Delete job")
    delete_parser.add_argument("job_id", help="Job ID")

    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up old jobs")
    cleanup_parser.add_argument("--days", type=int, default=7, help="Delete jobs older than N days")
    cleanup_parser.add_argument(
        "--delete-completed", action="store_true", help="Delete completed jobs too"
    )

    subparsers.add_parser("summary", help="Get job summary")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    manager = JobManager()

    if args.command == "list":
        status = JobStatus(args.status) if args.status else None
        jobs = asyncio.run(manager.list_jobs(status=status, limit=args.limit))

        print(f"{'Job ID':<20} {'Status':<12} {'Iterations':<12} {'Findings':<10} {'Created At'}")
        print("-" * 80)

        for job in jobs:
            print(
                f"{job.job_id:<20} {job.status.value:<12} "
                f"{job.iterations_completed}/{job.max_iterations:<6} "
                f"{job.findings_count:<10} {job.created_at}"
            )

    elif args.command == "get":
        job_result = asyncio.run(manager.get_job(args.job_id))
        if job_result is None:
            print(f"Job {args.job_id} not found")
        else:
            print(json.dumps(job_result.to_dict(), indent=2))

    elif args.command == "delete":
        asyncio.run(manager.delete_job(args.job_id))
        print(f"Deleted job {args.job_id}")

    elif args.command == "cleanup":
        deleted = manager.cleanup_old_jobs(days=args.days, keep_completed=not args.delete_completed)
        print(f"Deleted {deleted} jobs")

    elif args.command == "summary":
        summary = asyncio.run(manager.get_job_summary())
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

"""
Job manager for SHANDY discovery jobs.

Handles job lifecycle, status tracking, and cleanup.
"""

import json
import logging
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from shandy.exceptions import ProviderError
from shandy.orchestrator import create_job, run_discovery
from shandy.providers import get_provider

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Job status enum."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_FEEDBACK = "awaiting_feedback"  # Paused for scientist input (co-investigate mode)
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobInfo:
    """Job information."""

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
    use_skills: bool = True
    use_hypotheses: bool = False
    investigation_mode: str = "autonomous"  # "autonomous" or "coinvestigate"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobInfo":
        """Create from dictionary."""
        data = data.copy()
        data["status"] = JobStatus(data["status"])
        return cls(**data)


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
        self._running_jobs: Dict[str, threading.Thread] = {}
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
            ]:
                logger.warning(
                    "Marking stale job %s as cancelled (was %s)",
                    job_id,
                    job_info.status.value,
                )
                self._update_job_status(job_id, JobStatus.CANCELLED)
                stale_count += 1

        if stale_count > 0:
            logger.info("Cleaned up %d stale job(s) from previous run", stale_count)

    def create_job(
        self,
        job_id: str,
        research_question: str,
        data_files: List[Path],
        max_iterations: int = 10,
        use_skills: bool = True,
        use_hypotheses: bool = False,
        auto_start: bool = True,
        investigation_mode: str = "autonomous",
    ) -> JobInfo:
        """
        Create a new discovery job.

        Args:
            job_id: Unique job identifier
            research_question: Research question
            data_files: List of data file paths (can be empty for literature-only jobs)
            max_iterations: Maximum iterations
            use_skills: Whether to use skills
            use_hypotheses: Whether to enable hypothesis tracking tools
            auto_start: Whether to start job immediately
            investigation_mode: "autonomous" (default) or "coinvestigate"

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

        # Create job directory and files
        logger.info("Creating job %s", job_id)
        create_job(
            job_id=job_id,
            research_question=research_question,
            data_files=data_files,
            max_iterations=max_iterations,
            use_skills=use_skills,
            use_hypotheses=use_hypotheses,
            jobs_dir=self.jobs_dir,
            investigation_mode=investigation_mode,
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

        except Exception as e:  # noqa: BLE001 — thread-level safety net
            logger.error("Job %s failed: %s", job_id, e, exc_info=True)
            # Error already recorded in orchestrator

        finally:
            # Remove from running jobs
            with self._lock:
                self._running_jobs.pop(job_id, None)

            # Start next queued job if any
            self._start_next_queued_job()

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

        # Update status
        self._update_job_status(job_id, JobStatus.CANCELLED)

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
        self, status: Optional[JobStatus] = None, limit: Optional[int] = None
    ) -> List[JobInfo]:
        """
        List jobs.

        Args:
            status: Filter by status
            limit: Maximum number of jobs to return

        Returns:
            List of JobInfo objects, sorted by created_at (newest first)
        """
        jobs = []

        for job_id in self._list_job_ids():
            job_info = self._load_job_info(job_id)
            if job_info is None:
                continue

            # Filter by status
            if status is not None and job_info.status != status:
                continue

            jobs.append(job_info)

        # Sort by created_at (newest first)
        jobs.sort(key=lambda j: j.created_at, reverse=True)

        # Limit
        if limit is not None:
            jobs = jobs[:limit]

        return jobs

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
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)
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
            if created_at < cutoff:
                try:
                    self.delete_job(job_id)
                    deleted += 1
                except (OSError, ValueError) as e:
                    logger.error("Failed to delete job %s: %s", job_id, e)

        logger.info("Cleaned up %d old jobs", deleted)
        return deleted

    def get_running_jobs(self) -> List[JobInfo]:
        """Get list of currently running jobs."""
        return self.list_jobs(status=JobStatus.RUNNING)

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

    def get_job_summary(self) -> Dict[str, Any]:
        """
        Get summary of all jobs.

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

    def _list_job_ids(self) -> List[str]:
        """List all job IDs."""
        if not self.jobs_dir.exists():
            return []

        job_ids = []
        for item in self.jobs_dir.iterdir():
            if item.is_dir() and (item / "config.json").exists():
                job_ids.append(item.name)

        return job_ids

    def _load_job_info(self, job_id: str) -> Optional[JobInfo]:
        """Load job info from config.json."""
        config_path = self.jobs_dir / job_id / "config.json"

        if not config_path.exists():
            return None

        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            # For running jobs, get real-time progress from knowledge_state.json
            iterations_completed = config.get("iterations_completed", 0)
            findings_count = config.get("findings_count", 0)

            if config["status"] in ("running", "awaiting_feedback"):
                ks_path = self.jobs_dir / job_id / "knowledge_state.json"
                if ks_path.exists():
                    try:
                        with open(ks_path, encoding="utf-8") as f:
                            ks = json.load(f)
                        # KS iteration is the NEXT iteration to run, so completed = iteration - 1
                        ks_iteration = ks.get("iteration", 1)
                        iterations_completed = ks_iteration - 1 if ks_iteration > 1 else 0
                        findings_count = len(ks.get("findings", []))
                    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
                        logger.warning(
                            "Failed to load KS for running job %s: %s",
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
                use_skills=config.get("use_skills", True),
                use_hypotheses=config.get("use_hypotheses", False),
                investigation_mode=config.get("investigation_mode", "autonomous"),
            )

        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("Failed to load job info for %s: %s", job_id, e)
            return None

    def _update_job_status(self, job_id: str, status: JobStatus) -> None:
        """Update job status in config.json."""
        config_path = self.jobs_dir / job_id / "config.json"

        if not config_path.exists():
            return

        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            config["status"] = status.value

            # Update timestamps
            if status == JobStatus.RUNNING and "started_at" not in config:
                config["started_at"] = datetime.now().isoformat()
            elif status == JobStatus.COMPLETED and "completed_at" not in config:
                config["completed_at"] = datetime.now().isoformat()
            elif status == JobStatus.FAILED and "failed_at" not in config:
                config["failed_at"] = datetime.now().isoformat()
            elif status == JobStatus.CANCELLED:
                config["cancelled_at"] = datetime.now().isoformat()

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to update job status for %s: %s", job_id, e)


def main():
    """CLI entry point for job manager."""
    import argparse

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

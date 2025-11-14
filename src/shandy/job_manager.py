"""
Job manager for SHANDY discovery jobs.

Handles job lifecycle, status tracking, and cleanup.
"""

import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum

from .orchestrator import create_job, run_discovery
from .cost_tracker import get_budget_info, BudgetExceededError

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Job status enum."""
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
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
    max_iterations: int = 50
    iterations_completed: int = 0
    findings_count: int = 0
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    use_skills: bool = True

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

        logger.info(f"JobManager initialized: {self.jobs_dir}, max_concurrent={max_concurrent}")

    def create_job(
        self,
        job_id: str,
        research_question: str,
        data_files: List[Path],
        max_iterations: int = 50,
        use_skills: bool = True,
        auto_start: bool = True
    ) -> JobInfo:
        """
        Create a new discovery job.

        Args:
            job_id: Unique job identifier
            research_question: Research question
            data_files: List of data file paths
            max_iterations: Maximum iterations
            use_skills: Whether to use skills
            auto_start: Whether to start job immediately

        Returns:
            JobInfo object

        Raises:
            ValueError: If job_id already exists
            BudgetExceededError: If insufficient budget
        """
        # Check if job exists
        if self.get_job(job_id) is not None:
            raise ValueError(f"Job {job_id} already exists")

        # Check budget
        budget_info = get_budget_info()
        if budget_info["remaining_budget_usd"] < 1.0:
            raise BudgetExceededError(
                f"Insufficient budget: ${budget_info['remaining_budget_usd']:.2f} remaining"
            )

        # Create job directory and files
        logger.info(f"Creating job {job_id}")
        job_dir = create_job(
            job_id=job_id,
            research_question=research_question,
            data_files=data_files,
            max_iterations=max_iterations,
            use_skills=use_skills,
            jobs_dir=self.jobs_dir
        )

        # Load job info
        job_info = self._load_job_info(job_id)

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

            # Check concurrent limit
            if len(self._running_jobs) >= self.max_concurrent:
                # Queue the job
                self._update_job_status(job_id, JobStatus.QUEUED)
                logger.info(f"Job {job_id} queued (max concurrent reached)")
                return

            # Start the job
            logger.info(f"Starting job {job_id}")
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id,),
                daemon=True
            )
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
            logger.info(f"Running discovery for job {job_id}")
            result = run_discovery(job_dir)

            logger.info(f"Job {job_id} completed: {result}")

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
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
            if len(self._running_jobs) >= self.max_concurrent:
                return

            # Find next queued job
            for job_id in self._list_job_ids():
                job_info = self.get_job(job_id)
                if job_info and job_info.status == JobStatus.QUEUED:
                    logger.info(f"Starting queued job {job_id}")
                    thread = threading.Thread(
                        target=self._run_job,
                        args=(job_id,),
                        daemon=True
                    )
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

        # Note: We can't actually kill the thread cleanly in Python
        # The orchestrator will check status and stop at next iteration
        logger.info(f"Job {job_id} cancelled (will stop at next iteration)")

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
        limit: Optional[int] = None
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
            logger.info(f"Deleted job {job_id}")

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
                except Exception as e:
                    logger.error(f"Failed to delete job {job_id}: {e}")

        logger.info(f"Cleaned up {deleted} old jobs")
        return deleted

    def get_running_jobs(self) -> List[JobInfo]:
        """Get list of currently running jobs."""
        return self.list_jobs(status=JobStatus.RUNNING)

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

        total_cost = sum(j.cost_usd for j in jobs if j.cost_usd is not None)
        budget_info = get_budget_info()

        return {
            "total_jobs": len(jobs),
            "status_counts": status_counts,
            "total_cost_usd": total_cost,
            "budget_info": budget_info
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
            with open(config_path) as f:
                config = json.load(f)

            return JobInfo(
                job_id=config["job_id"],
                research_question=config["research_question"],
                status=JobStatus(config["status"]),
                created_at=config["created_at"],
                started_at=config.get("started_at"),
                completed_at=config.get("completed_at"),
                failed_at=config.get("failed_at"),
                max_iterations=config["max_iterations"],
                iterations_completed=config.get("iterations_completed", 0),
                findings_count=config.get("findings_count", 0),
                cost_usd=config.get("final_cost_usd"),
                error=config.get("error"),
                use_skills=config.get("use_skills", True)
            )

        except Exception as e:
            logger.error(f"Failed to load job info for {job_id}: {e}")
            return None

    def _update_job_status(self, job_id: str, status: JobStatus) -> None:
        """Update job status in config.json."""
        config_path = self.jobs_dir / job_id / "config.json"

        if not config_path.exists():
            return

        try:
            with open(config_path) as f:
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

            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to update job status for {job_id}: {e}")


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
    cleanup_parser.add_argument("--delete-completed", action="store_true", help="Delete completed jobs too")

    # Summary
    subparsers.add_parser("summary", help="Get job summary")

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Create job manager
    manager = JobManager()

    # Execute command
    if args.command == "list":
        status = JobStatus(args.status) if args.status else None
        jobs = manager.list_jobs(status=status, limit=args.limit)

        print(f"{'Job ID':<20} {'Status':<12} {'Iterations':<12} {'Findings':<10} {'Cost':<10} {'Created At'}")
        print("-" * 90)

        for job in jobs:
            cost_str = f"${job.cost_usd:.2f}" if job.cost_usd else "N/A"
            print(f"{job.job_id:<20} {job.status.value:<12} "
                  f"{job.iterations_completed}/{job.max_iterations:<6} "
                  f"{job.findings_count:<10} {cost_str:<10} {job.created_at}")

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
        deleted = manager.cleanup_old_jobs(
            days=args.days,
            keep_completed=not args.delete_completed
        )
        print(f"Deleted {deleted} jobs")

    elif args.command == "summary":
        summary = manager.get_job_summary()
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

"""CLI entry point for job manager."""

import argparse
import json
import logging
from pathlib import Path

from openscientist.job.types import JobStatus
from openscientist.job_manager import JobManager


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

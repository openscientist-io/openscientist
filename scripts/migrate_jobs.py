#!/usr/bin/env python3
"""
Migration script for SHANDY jobs.

Migrates jobs from the old naming convention to the new one:
- knowledge_graph.json -> knowledge_state.json
- plots/ -> provenance/
- Adds missing fields (e.g., strapline to iteration_summaries)

Usage:
    python scripts/migrate_jobs.py [--jobs-dir PATH] [--dry-run] [--no-backup]

Options:
    --jobs-dir PATH   Jobs directory (default: jobs)
    --dry-run         Show what would be done without making changes
    --no-backup       Skip creating backup (not recommended)
"""

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


def migrate_job(job_dir: Path, dry_run: bool = False) -> dict:
    """
    Migrate a single job directory.

    Args:
        job_dir: Path to job directory
        dry_run: If True, don't make changes

    Returns:
        Dict with migration results
    """
    results = {"job_id": job_dir.name, "changes": [], "errors": [], "skipped": False}

    # Check if already migrated
    if (job_dir / "knowledge_state.json").exists():
        results["skipped"] = True
        results["changes"].append("Already migrated (knowledge_state.json exists)")
        return results

    # 1. Rename knowledge_graph.json -> knowledge_state.json
    old_ks = job_dir / "knowledge_graph.json"
    new_ks = job_dir / "knowledge_state.json"

    if old_ks.exists():
        if not dry_run:
            old_ks.rename(new_ks)
        results["changes"].append("Renamed knowledge_graph.json -> knowledge_state.json")

        # Add missing fields to the knowledge state
        try:
            with open(new_ks if not dry_run else old_ks, encoding="utf-8") as f:
                ks_data = json.load(f)

            modified = False

            # Ensure iteration_summaries exists
            if "iteration_summaries" not in ks_data:
                ks_data["iteration_summaries"] = []
                modified = True
                results["changes"].append("Added missing iteration_summaries field")

            # Ensure each iteration_summary has strapline
            for summary in ks_data.get("iteration_summaries", []):
                if "strapline" not in summary:
                    summary["strapline"] = ""
                    modified = True

            if modified and any(
                "strapline" not in s for s in ks_data.get("iteration_summaries", [])
            ):
                results["changes"].append("Added missing strapline fields to iteration_summaries")

            # Ensure feedback_history exists
            if "feedback_history" not in ks_data:
                ks_data["feedback_history"] = []
                modified = True
                results["changes"].append("Added missing feedback_history field")

            # Write back if modified
            if modified and not dry_run:
                with open(new_ks, "w", encoding="utf-8") as f:
                    json.dump(ks_data, f, indent=2)

        except json.JSONDecodeError as e:
            results["errors"].append(f"Invalid JSON in knowledge state: {e}")
        except Exception as e:
            results["errors"].append(f"Error updating knowledge state: {e}")
    else:
        results["errors"].append("knowledge_graph.json not found")

    # 2. Rename plots/ -> provenance/
    old_plots = job_dir / "plots"
    new_provenance = job_dir / "provenance"

    if old_plots.exists() and not new_provenance.exists():
        if not dry_run:
            old_plots.rename(new_provenance)
        results["changes"].append("Renamed plots/ -> provenance/")
    elif old_plots.exists() and new_provenance.exists():
        # Both exist - merge contents
        if not dry_run:
            for item in old_plots.iterdir():
                dest = new_provenance / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
            # Remove old plots dir if empty
            try:
                old_plots.rmdir()
            except OSError:
                pass  # Not empty
        results["changes"].append("Merged plots/ into existing provenance/")

    return results


def migrate_all_jobs(jobs_dir: Path, dry_run: bool = False, create_backup: bool = True):
    """
    Migrate all jobs in the jobs directory.

    Args:
        jobs_dir: Path to jobs directory
        dry_run: If True, don't make changes
        create_backup: If True, create backup before migration
    """
    if not jobs_dir.exists():
        print(f"Jobs directory does not exist: {jobs_dir}")
        return

    # Count jobs
    job_dirs = [d for d in jobs_dir.iterdir() if d.is_dir() and (d / "config.json").exists()]
    print(f"Found {len(job_dirs)} jobs to migrate")

    if not job_dirs:
        print("No jobs to migrate")
        return

    # Create backup
    if create_backup and not dry_run:
        backup_dir = jobs_dir.parent / f"jobs_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print(f"Creating backup at {backup_dir}...")
        shutil.copytree(jobs_dir, backup_dir)
        print("Backup created successfully")

    # Migrate each job
    migrated = 0
    skipped = 0
    errors = 0

    for job_dir in sorted(job_dirs):
        results = migrate_job(job_dir, dry_run=dry_run)

        if results["skipped"]:
            skipped += 1
            if not dry_run:
                print(f"  {results['job_id']}: Skipped (already migrated)")
        elif results["errors"]:
            errors += 1
            print(f"  {results['job_id']}: ERRORS")
            for error in results["errors"]:
                print(f"    - {error}")
        else:
            migrated += 1
            print(f"  {results['job_id']}: {'Would migrate' if dry_run else 'Migrated'}")
            for change in results["changes"]:
                print(f"    - {change}")

    # Summary
    print()
    print("=" * 50)
    print(f"Migration {'preview' if dry_run else 'complete'}:")
    print(f"  Migrated: {migrated}")
    print(f"  Skipped (already migrated): {skipped}")
    print(f"  Errors: {errors}")

    if dry_run:
        print()
        print("This was a dry run. No changes were made.")
        print("Run without --dry-run to apply changes.")


def main():
    parser = argparse.ArgumentParser(description="Migrate SHANDY jobs to new naming convention")
    parser.add_argument(
        "--jobs-dir", type=Path, default=Path("jobs"), help="Jobs directory (default: jobs)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="Skip creating backup (not recommended)"
    )

    args = parser.parse_args()

    print("SHANDY Jobs Migration")
    print(f"Jobs directory: {args.jobs_dir}")
    print(f"Dry run: {args.dry_run}")
    print(f"Create backup: {not args.no_backup}")
    print()

    migrate_all_jobs(jobs_dir=args.jobs_dir, dry_run=args.dry_run, create_backup=not args.no_backup)


if __name__ == "__main__":
    main()

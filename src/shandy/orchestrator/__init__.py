"""
Orchestrator subpackage for SHANDY discovery.

Module layout:
- discovery.py  — run_discovery_async(), sync_knowledge_state_to_db(), helpers
- iteration.py  — prompt builders, increment_ks_iteration, update_job_status
- setup.py      — create_job (filesystem initialization)

All public names are re-exported here for backward compatibility so that
existing code using ``from shandy.orchestrator import create_job, run_discovery``
continues to work.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

# Re-export all public names from submodules
from shandy.orchestrator.discovery import (
    get_version_metadata,
    regenerate_report_async,
    run_discovery_async,
    sync_knowledge_state_to_db,
)
from shandy.orchestrator.iteration import (
    increment_ks_iteration,
    update_job_status,
    wait_for_feedback_or_timeout,
)
from shandy.orchestrator.setup import create_job

logger = logging.getLogger(__name__)


def run_discovery(job_dir: Path) -> dict[str, Any]:
    """
    Run autonomous discovery (synchronous wrapper around run_discovery_async).

    Called by JobManager._run_job() in a worker thread.  Creates its own
    asyncio event loop via asyncio.run().

    Args:
        job_dir: Path to job directory

    Returns:
        Dict: {job_id, status, iterations, findings}
    """
    return asyncio.run(run_discovery_async(Path(job_dir)))


def regenerate_report(job_dir: Path) -> dict[str, Any]:
    """
    Re-run report generation (synchronous wrapper around regenerate_report_async).

    Called by JobManager._run_regenerate_report() in a worker thread.

    Args:
        job_dir: Path to job directory

    Returns:
        Dict: {job_id, status, report_success}
    """
    return asyncio.run(regenerate_report_async(Path(job_dir)))


__all__ = [
    "create_job",
    "regenerate_report",
    "regenerate_report_async",
    "run_discovery",
    "run_discovery_async",
    "sync_knowledge_state_to_db",
    "increment_ks_iteration",
    "update_job_status",
    "wait_for_feedback_or_timeout",
    "get_version_metadata",
]

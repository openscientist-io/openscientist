"""
Orchestrator subpackage for OpenScientist discovery.

Module layout:
- discovery.py  — run_discovery_async(), helpers
- iteration.py  — prompt builders, increment_ks_iteration, update_job_status
- setup.py      — create_job (filesystem initialization)

All public names are re-exported here to keep a stable import surface
for ``from openscientist.orchestrator import create_job, run_discovery``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

# Re-export all public names from submodules
from openscientist.orchestrator.discovery import (
    get_version_metadata,
    run_discovery_async,
)
from openscientist.orchestrator.iteration import (
    increment_ks_iteration,
    update_job_status,
    wait_for_feedback_or_timeout,
)
from openscientist.orchestrator.setup import create_job

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


__all__ = [
    "create_job",
    "get_version_metadata",
    "increment_ks_iteration",
    "run_discovery",
    "run_discovery_async",
    "update_job_status",
    "wait_for_feedback_or_timeout",
]

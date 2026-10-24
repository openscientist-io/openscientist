"""
Orchestrator subpackage for OpenScientist discovery.

Module layout:
- discovery.py  — run_discovery_async(), helpers
- iteration.py  — prompt builders, increment_ks_iteration, update_job_status
- setup.py      — create_job (filesystem initialization)

All public names are re-exported here to keep a stable import surface
for ``from openscientist.orchestrator import create_job, run_discovery_async``.
"""

from __future__ import annotations

import logging

# Re-export all public names from submodules
from openscientist.orchestrator.discovery import (
    get_version_metadata,
    regenerate_report_async,
    run_discovery_async,
)
from openscientist.orchestrator.iteration import (
    increment_ks_iteration,
    update_job_status,
    wait_for_feedback_or_timeout,
)
from openscientist.orchestrator.setup import create_job

logger = logging.getLogger(__name__)


__all__ = [
    "create_job",
    "get_version_metadata",
    "increment_ks_iteration",
    "regenerate_report_async",
    "run_discovery_async",
    "update_job_status",
    "wait_for_feedback_or_timeout",
]

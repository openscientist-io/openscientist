#!/usr/bin/env python3
"""
Agent container entrypoint for Open Scientist.

This script runs inside the open_scientist-agent Docker container and executes
the discovery loop for a single job.  It is the ONLY code running in
the container — there is no web server or NiceGUI UI.

Required environment variables:
  JOB_ID      — UUID of the job to run
  JOB_DIR     — Path to the job directory (mounted volume, default /agent/job)
  DATABASE_URL — PostgreSQL connection string (for status updates and KS sync)
  CLAUDE_PROVIDER / ANTHROPIC_API_KEY / etc. — provider credentials

The container:
1. Calls run_discovery_async(job_dir) from orchestrator/discovery.py
2. The discovery loop uses SDKAgentExecutor (CLAUDE_PROVIDER=anthropic)
3. Status is written to PostgreSQL; the web server reads it from there
4. On completion (success or failure), the process exits with code 0 or 1
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Set up logging before any imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("agent-entrypoint")


async def main() -> int:
    """Run discovery for the job specified in environment variables."""
    job_id = os.environ.get("JOB_ID")  # noqa: env-ok
    job_dir_str = os.environ.get("JOB_DIR", "/agent/job")  # noqa: env-ok

    if not job_id:
        logger.error("JOB_ID environment variable is required")
        return 1

    job_dir = Path(job_dir_str)
    if not job_dir.exists():
        logger.error("Job directory does not exist: %s", job_dir)
        return 1

    logger.info("Starting agent for job %s in %s", job_id, job_dir)

    try:
        from open_scientist.orchestrator.discovery import run_discovery_async

        result = await run_discovery_async(job_dir)
        logger.info(
            "Job %s completed: status=%s, iterations=%d, findings=%d",
            job_id,
            result.get("status"),
            result.get("iterations", 0),
            result.get("findings", 0),
        )
        return 0 if result.get("status") == "completed" else 1

    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

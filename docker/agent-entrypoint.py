#!/usr/bin/env python3
"""
Agent container entrypoint for OpenScientist.

This script runs inside the openscientist-agent Docker container and executes
the discovery loop for a single job.  It is the ONLY code running in
the container — there is no web server or NiceGUI UI.

Required environment variables:
  JOB_ID      — UUID of the job to run
  JOB_DIR     — Path to the job directory (mounted volume, default /agent/job)
  DATABASE_URL — PostgreSQL connection string (for status updates and KS sync)
  OPENSCIENTIST_PROVIDER / ANTHROPIC_API_KEY / etc. - provider credentials

The container:
1. Calls run_discovery_async(job_dir) from orchestrator/discovery.py
2. The discovery loop builds the agent via the provider/agent factory
   (OPENSCIENTIST_PROVIDER selects the backend)
3. Status is written to PostgreSQL; the web server reads it from there
4. On completion (success or failure), the process exits with code 0 or 1
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

# Files the harness and its children write into the job tree must stay group
# readable and never world readable, even though the kernel umask is 0022.
os.umask(0o007)

# Set up logging before any imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("agent-entrypoint")


def trust_job_repository(job_dir: Path) -> None:
    """Mark the job's repository as safe for the run user's git, since the web
    inits it as its own uid and git refuses a repository the current user does
    not own."""
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", str(job_dir)],
        check=False,
        capture_output=True,
    )


async def main() -> int:
    """Run discovery for the job specified in environment variables."""
    job_id = os.environ.get("JOB_ID")  # noqa: env-ok
    job_dir_str = os.environ.get("JOB_DIR", "/agent/job")  # noqa: env-ok
    # "report_only" re-runs just the report-generation phase against the
    # already-persisted findings (admin "Regenerate report"). Default is the
    # full discovery loop.
    run_mode = os.environ.get("OPENSCIENTIST_RUN_MODE", "discovery")  # noqa: env-ok

    if not job_id:
        logger.error("JOB_ID environment variable is required")
        return 1

    job_dir = Path(job_dir_str)
    if not job_dir.exists():
        logger.error("Job directory does not exist: %s", job_dir)
        return 1

    # git would otherwise treat the web-owned repository as dubious ownership.
    trust_job_repository(job_dir)

    logger.info("Starting agent for job %s in %s (mode=%s)", job_id, job_dir, run_mode)

    # docker stop sends SIGTERM, which Python as PID 1 would ignore until the
    # SIGKILL. Cancelling the run instead lets its cleanup write the cost.
    main_task = asyncio.current_task()
    if main_task is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)

    try:
        from openscientist.job.agent_error import write_agent_error
        from openscientist.orchestrator.discovery import (
            regenerate_report_async,
            run_discovery_async,
        )

        if run_mode == "chat":
            from openscientist.job_chat import CHAT_TURN_ENV, run_chat_turn_async

            result = await run_chat_turn_async(
                job_dir,
                os.environ[CHAT_TURN_ENV],  # noqa: env-ok
                job_id,
            )
        elif run_mode == "report_only":
            result = await regenerate_report_async(job_dir)
        else:
            result = await run_discovery_async(job_dir)
        logger.info(
            "Job %s completed: status=%s, iterations=%d, findings=%d",
            job_id,
            result.get("status"),
            result.get("iterations", 0),
            result.get("findings", 0),
        )
        if result.get("status") == "completed":
            return 0
        # The web sees only the exit code, so leave it the reason.
        if result.get("error"):
            write_agent_error(job_dir, str(result["error"]))
        return 1

    except asyncio.CancelledError:
        # The web stopped the container and has already moved the job on.
        logger.info("Job %s stopped", job_id)
        return 1

    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        write_agent_error(job_dir, f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

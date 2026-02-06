"""
NiceGUI web interface for SHANDY.

Provides web UI for job submission, monitoring, and results viewing.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from nicegui import app, ui

from shandy.job_manager import JobManager

# Load environment variables from .env file
# Try Docker path first, fall back to local path
if not load_dotenv("/app/.env", override=True):
    load_dotenv(".env", override=True)

logger = logging.getLogger(__name__)

# Storage secret for NiceGUI app.storage.user
STORAGE_SECRET = os.getenv("STORAGE_SECRET", "change-this-to-a-random-secret-string-in-production")


class _AppState:
    """Module-level singleton holding mutable app state (avoids bare globals)."""

    def __init__(self):
        self.job_manager: Optional[JobManager] = None
        self.jobs_dir: Path = Path("jobs")


_state = _AppState()

# Import page modules to register routes
# Must be imported after _state is defined so pages can access it
from shandy.webapp_components import pages  # noqa: E402, F401


def get_job_manager() -> JobManager:
    """
    Get the global job manager instance, initializing if needed.

    This handles cases where the module is reloaded (e.g., in dev mode with --reload)
    and the global variable is reset.

    Returns:
        The global JobManager instance.
    """
    if _state.job_manager is None:
        logger.warning("Job manager was None, initializing now (likely due to module reload)")
        _state.job_manager = JobManager(jobs_dir=_state.jobs_dir, max_concurrent=1)
        # Add static file serving
        try:
            app.add_static_files("/jobs", str(_state.jobs_dir))
        except (RuntimeError, ValueError) as e:
            logger.debug("Static files already registered: %s", e)
    return _state.job_manager


def init_app(jobs_dir: Path = Path("jobs"), max_concurrent: int = 1):
    """Initialize the web application."""
    _state.jobs_dir = jobs_dir

    if _state.job_manager is not None:
        logger.info("Web app already initialized, skipping re-initialization")
        return

    _state.job_manager = JobManager(jobs_dir=jobs_dir, max_concurrent=max_concurrent)

    # Add static file serving for job plots
    app.add_static_files("/jobs", str(jobs_dir))

    logger.info("Web app initialized with jobs_dir=%s", jobs_dir)


def main(
    host: str = "0.0.0.0",
    port: int = 8080,
    jobs_dir: Path = Path("jobs"),
    reload: bool = False,
):
    """
    Run the web application.

    Args:
        host: Host to bind to
        port: Port to bind to
        jobs_dir: Directory for jobs
        reload: Enable auto-reload on file changes (development mode)
    """
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Initialize app BEFORE ui.run() to ensure job_manager is set
    # This must happen before any page is accessed
    init_app(jobs_dir=jobs_dir)

    logger.info("Starting NiceGUI server on %s:%s (reload=%s)", host, port, reload)

    # Run NiceGUI
    ui.run(
        host=host,
        port=port,
        title="SHANDY",
        reload=reload,  # Enable auto-reload in development mode
        show=False,  # Don't auto-open browser in Docker
        storage_secret=STORAGE_SECRET,  # Required for app.storage.user
    )


if __name__ in {"__main__", "__mp_main__"}:
    import argparse

    parser = argparse.ArgumentParser(description="SHANDY Web Interface")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--jobs-dir", default="jobs", help="Jobs directory")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on file changes (development mode)",
    )

    args = parser.parse_args()

    main(host=args.host, port=args.port, jobs_dir=Path(args.jobs_dir), reload=args.reload)

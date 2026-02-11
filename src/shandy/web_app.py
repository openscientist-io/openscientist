"""
NiceGUI web interface for SHANDY.

Provides web UI for job submission, monitoring, and results viewing.
"""

import logging
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

# Validate settings at import time (but don't fail yet - defer to main())
_settings_error: Optional[str] = None
try:
    from shandy.settings import get_settings

    _loaded_settings = get_settings()
    STORAGE_SECRET = _loaded_settings.auth.storage_secret
except Exception as e:
    _settings_error = str(e)
    # Fallback for import-time usage - use a sentinel value
    STORAGE_SECRET = "change-this-to-a-random-secret-string-in-production"


def _create_config_error_page(error_message: str):
    """Create an error page for configuration errors.

    This page is shown when required configuration keys are missing,
    allowing deployers to see the error in the UI rather than just in logs.
    """

    @ui.page("/")
    @ui.page("/{path:path}")
    def config_error_page(path: str = ""):
        """Display configuration error with 500 status."""
        app.storage.user["_error_shown"] = True

        with ui.column().classes("absolute-center items-center gap-6 max-w-2xl p-8"):
            # Error icon
            ui.icon("error", size="80px", color="red")

            # Title
            ui.markdown("# Configuration Error").classes("text-red-600")

            # Subtitle
            ui.markdown("_SHANDY cannot start due to missing or invalid configuration._").classes(
                "text-gray-600"
            )

            ui.separator().classes("w-full")

            # Error details card
            with ui.card().classes("w-full bg-red-50 border-l-4 border-red-500"):
                ui.label("Error Details").classes("font-bold text-red-800 mb-2")
                ui.code(error_message).classes("w-full text-sm whitespace-pre-wrap break-words")

            ui.separator().classes("w-full")

            # Instructions
            with ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-500"):
                ui.label("How to Fix").classes("font-bold text-blue-800 mb-2")
                ui.markdown(
                    """
1. **Check your `.env` file** - Ensure all required environment variables are set
2. **Review the error message above** - It indicates which configuration is missing
3. **Restart the application** after fixing the configuration

For setup instructions, see the project's `README.md` or `CONTRIBUTING.md`.
                    """
                ).classes("text-blue-700")

            # HTTP 500 indicator
            ui.label("HTTP 500 - Internal Server Error").classes("text-gray-400 text-sm mt-4")

    # Also add a health check endpoint that returns 500
    from fastapi import Response

    @app.get("/health")
    def health_check():
        return Response(
            content='{"status": "error", "message": "Configuration error"}',
            status_code=500,
            media_type="application/json",
        )


class _AppState:
    """Module-level singleton holding mutable app state (avoids bare globals)."""

    def __init__(self):
        self.job_manager: Optional[JobManager] = None
        self.jobs_dir: Path = Path("jobs")


_state = _AppState()


def _register_oauth_routes():
    """Register OAuth authentication routes with the underlying FastAPI app."""
    try:
        from shandy.auth.fastapi_routes import router as auth_router

        # NiceGUI's app is a FastAPI app, so we can mount routers
        app.include_router(auth_router)
        logger.info("OAuth authentication routes registered")
    except Exception as e:
        logger.warning("Failed to register OAuth routes: %s", e)


def _register_api_routes():
    """Register REST API routes with the underlying FastAPI app."""
    try:
        from shandy.api import api_router

        # Mount the REST API
        app.include_router(api_router)
        logger.info("REST API routes registered at /api/v1")
    except Exception as e:
        logger.warning("Failed to register API routes: %s", e)


def _register_share_routes():
    """Register web share routes for session-based job sharing."""
    try:
        from shandy.webapp_components.share_routes import router as share_router

        # Mount the share routes
        app.include_router(share_router)
        logger.info("Share routes registered at /web/shares")
    except Exception as e:
        logger.warning("Failed to register share routes: %s", e)


# Import page modules to register routes
# Must be imported after _state is defined so pages can access it
# Import auth routes first
_register_oauth_routes()
_register_api_routes()
_register_share_routes()

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

    # Initialize database connection
    try:
        import asyncio

        from shandy.database.engine import get_engine

        engine = get_engine()

        async def verify_db():
            """Verify database connection and tables exist."""
            try:
                async with engine.begin() as conn:
                    # Simple query to verify connection
                    await conn.execute("SELECT 1")
                logger.info("Database connection verified")
            except Exception as e:
                logger.error("Database connection failed: %s", e)
                logger.warning("Application will continue but database features may not work")

        async def start_background_tasks():
            """Start background tasks after database verification."""
            await verify_db()
            # Start skill sync scheduler
            try:
                from shandy.skill_scheduler import start_skill_scheduler

                await start_skill_scheduler()
                logger.info("Skill sync scheduler started")
            except Exception as e:
                logger.warning("Failed to start skill sync scheduler: %s", e)

        # Run verification and background tasks
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If loop is already running (NiceGUI context), schedule as task
                asyncio.create_task(start_background_tasks())
            else:
                loop.run_until_complete(start_background_tasks())
        except RuntimeError:
            # Create new event loop if none exists
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(start_background_tasks())

    except Exception as e:
        logger.error("Failed to initialize database: %s", e)
        logger.warning("Application will continue but database features may not work")

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

    # Check for configuration errors
    if _settings_error is not None:
        logger.error("Configuration error: %s", _settings_error)
        logger.info("Starting minimal error page to display configuration error in UI")

        # Create the error page and run with minimal config
        _create_config_error_page(_settings_error)

        ui.run(
            host=host,
            port=port,
            title="SHANDY - Configuration Error",
            reload=False,  # No reload in error mode
            show=False,
            storage_secret=STORAGE_SECRET,
        )
        return  # Exit after running error mode

    logger.info("Settings validated successfully")

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

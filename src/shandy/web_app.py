"""
NiceGUI web interface for SHANDY.

Provides web UI for job submission, monitoring, and results viewing.
"""

import argparse
import importlib
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from nicegui import app, ui

from shandy.job_manager import JobManager
from shandy.version import get_version_string

# Path to assets directory (favicon, icons, etc.)
ASSETS_DIR = Path(__file__).parent / "assets"


# ── NiceGUI patch: silence "parent slot deleted" timer errors ──────────────
# When a container (e.g. feedback_container) is .clear()-ed, child timers
# lose their parent slot but keep firing until on_disconnect cleanup runs.
# The base timer's inner try/except only covers the *callback*, not the
# `with self._get_context():` call at line 90 of timer.py, so the error
# propagates to the background task handler and fills the log.
# Patch: return nullcontext() and deactivate the timer instead of raising.
def _patch_nicegui_timer() -> None:
    from contextlib import nullcontext

    from nicegui.elements.timer import Timer as _NiceGUITimer

    _orig = _NiceGUITimer._get_context

    def _safe_get_context(self):  # type: ignore[no-untyped-def]
        try:
            return _orig(self)
        except RuntimeError:
            self.deactivate()
            return nullcontext()

    _NiceGUITimer._get_context = _safe_get_context  # type: ignore[method-assign]


_patch_nicegui_timer()
# ─────────────────────────────────────────────────────────────────────────────

# Load environment variables from .env file
# Try Docker path first, fall back to local path
# Use override=False so Docker/system env vars take precedence over .env
if not load_dotenv("/app/.env", override=False):
    load_dotenv(".env", override=False)

logger = logging.getLogger(__name__)

# Validate settings at import time (but don't fail yet - defer to main())
_settings_error: str | None = None
try:
    from shandy.settings import get_settings

    _loaded_settings = get_settings()
    STORAGE_SECRET = _loaded_settings.auth.storage_secret
except Exception as e:
    _settings_error = str(e)
    # Fallback for import-time usage — app will show config error page
    STORAGE_SECRET = "unconfigured-fallback"


def _create_config_error_page(error_message: str) -> None:
    """Create an error page for configuration errors.

    This page is shown when required configuration keys are missing.
    For security, only a generic error is shown to users - full details
    are logged server-side for administrators.
    """
    # Log full error details server-side for administrators
    logger.error("Server configuration error (details hidden from UI): %s", error_message)

    @ui.page("/")
    @ui.page("/{path:path}")
    def config_error_page(_path: str = "") -> None:
        """Display generic server error with 500 status."""
        app.storage.user["_error_shown"] = True

        with ui.column().classes("absolute-center items-center gap-6 max-w-lg p-8"):
            # Error icon
            ui.icon("error", size="80px", color="red")

            # Title
            ui.markdown("# Server Error").classes("text-red-600")

            # Generic message - no internal details exposed
            ui.markdown(
                "_The server encountered a configuration error and cannot process requests._"
            ).classes("text-gray-600 text-center")

            ui.separator().classes("w-full")

            # Instructions card
            with ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-500"):
                ui.label("What to do").classes("font-bold text-blue-800 mb-2")
                ui.markdown(
                    """
Please contact your **system administrator** to resolve this issue.

Administrators can find detailed error information in the server logs.
                    """
                ).classes("text-blue-700")

            # HTTP 500 indicator
            ui.label("HTTP 500 - Internal Server Error").classes("text-gray-400 text-sm mt-4")

    # Also add a health check endpoint that returns 500
    @app.get("/health")
    def health_check() -> Response:
        return Response(
            content='{"status": "error", "message": "Server error"}',
            status_code=500,
            media_type="application/json",
        )


class _AppState:
    """Module-level singleton holding mutable app state (avoids bare globals)."""

    def __init__(self) -> None:
        self.job_manager: JobManager | None = None
        self.jobs_dir: Path = Path("jobs")


_state = _AppState()


def _register_oauth_routes() -> None:
    """Register OAuth authentication routes with the underlying FastAPI app."""
    try:
        from shandy.auth.fastapi_routes import router as auth_router

        # NiceGUI's app is a FastAPI app, so we can mount routers
        app.include_router(auth_router)
        logger.info("OAuth authentication routes registered")
    except Exception as e:
        logger.warning("Failed to register OAuth routes: %s", e)


def _register_api_routes() -> None:
    """Register REST API routes with the underlying FastAPI app."""
    try:
        from shandy.api import api_router

        # Mount the REST API
        app.include_router(api_router)
        logger.info("REST API routes registered at /api/v1")
    except Exception as e:
        logger.warning("Failed to register API routes: %s", e)


def _register_share_routes() -> None:
    """Register web share routes for session-based job sharing."""
    try:
        from shandy.webapp_components.share_routes import router as share_router

        # Mount the share routes
        app.include_router(share_router)
        logger.info("Share routes registered at /web/shares")
    except Exception as e:
        logger.warning("Failed to register share routes: %s", e)


# Configure OpenAPI metadata
app.title = "SHANDY API"
app.version = "1.0.0"
app.description = "REST API for Scientific Hypothesis Agent for Novel Discovery"


def _register_openapi_docs() -> None:
    """Register OpenAPI documentation routes (Swagger UI and ReDoc).

    NiceGUI disables FastAPI's built-in docs, so we add them manually.
    """

    @app.get("/api-docs", include_in_schema=False)
    async def swagger_ui_html() -> HTMLResponse:
        """Swagger UI documentation."""
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    @app.get("/api-redoc", include_in_schema=False)
    async def redoc_html() -> HTMLResponse:
        """ReDoc documentation."""
        return get_redoc_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - ReDoc",
        )

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_json() -> JSONResponse:
        """OpenAPI schema as JSON, filtered to only include /api/v1/* paths."""
        schema = app.openapi()
        # Filter to only include API paths
        filtered_paths = {
            path: ops for path, ops in schema.get("paths", {}).items() if path.startswith("/api/")
        }
        filtered_schema = {**schema, "paths": filtered_paths}
        return JSONResponse(filtered_schema)

    logger.info("OpenAPI documentation registered at /api-docs and /api-redoc")


_register_openapi_docs()


# Lightweight health endpoint — returns JSON without touching NiceGUI storage,
# so the Docker health check doesn't trigger watchfiles reload events.
@app.get("/health", include_in_schema=False)
def health_check() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# Import page modules to register routes
# Must be imported after _state is defined so pages can access it
# Import auth routes first
_register_oauth_routes()
_register_api_routes()
_register_share_routes()
importlib.import_module("shandy.webapp_components.pages")


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
        _state.job_manager = JobManager(
            jobs_dir=_state.jobs_dir,
            max_concurrent=get_settings().max_concurrent_jobs,
        )
        # Add static file serving
        try:
            app.add_static_files("/jobs", str(_state.jobs_dir))
        except (RuntimeError, ValueError) as e:
            logger.debug("Static files already registered: %s", e)
    return _state.job_manager


async def _verify_db_connection_and_rls(engine: Any) -> None:
    """Verify database connection, tables, and RLS configuration."""
    from sqlalchemy import text

    try:
        async with engine.begin() as conn:
            # Simple query to verify connection
            await conn.execute(text("SELECT 1"))
            logger.info("Database connection verified")

            # Verify RLS is properly configured
            rls_result = await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'jobs'"
                )
            )
            rls_row = rls_result.first()
            if rls_row is None:
                logger.warning("RLS CHECK: 'jobs' table not found — migrations may not have run")
            elif not rls_row[0] or not rls_row[1]:
                logger.error(
                    "RLS CHECK FAILED: jobs table has rowsecurity=%s, forcerowsecurity=%s — "
                    "run 'alembic upgrade head' to fix",
                    rls_row[0],
                    rls_row[1],
                )
            else:
                logger.info("RLS CHECK: jobs table has RLS enabled and forced")

            # Verify shandy_app role exists and is not superuser
            role_result = await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'shandy_app'")
            )
            role_row = role_result.first()
            if role_row is None:
                logger.error(
                    "RLS CHECK FAILED: 'shandy_app' role does not exist — "
                    "run 'alembic upgrade head' to create it"
                )
            elif role_row[0]:
                logger.error(
                    "RLS CHECK FAILED: 'shandy_app' role is a SUPERUSER — "
                    "this bypasses all RLS policies"
                )
            elif role_row[1]:
                logger.error(
                    "RLS CHECK FAILED: 'shandy_app' role has BYPASSRLS — "
                    "this bypasses all RLS policies"
                )
            else:
                logger.info("RLS CHECK: shandy_app role is correctly configured")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        logger.warning("Application will continue but database features may not work")


async def _ensure_default_skill_sources() -> None:
    """Ensure default skill sources exist in the database."""
    from sqlalchemy import select

    from shandy.database.models import SkillSource
    from shandy.database.session import get_admin_session

    default_sources = [
        {
            "source_type": "github",
            "name": "Claude Scientific Skills",
            "url": "https://github.com/K-Dense-AI/claude-scientific-skills",
            "branch": "main",
            "skills_path": "scientific-skills",
            "is_enabled": True,
        },
        {
            "source_type": "local",
            "name": "SHANDY Built-in Skills",
            "path": "skills",
            "is_enabled": True,
        },
    ]

    try:
        async with get_admin_session() as session:
            for source_data in default_sources:
                # Check if source already exists by URL or path
                if source_data.get("url"):
                    stmt = select(SkillSource).where(SkillSource.url == source_data["url"])
                else:
                    stmt = select(SkillSource).where(SkillSource.path == source_data.get("path"))
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if not existing:
                    source = SkillSource(**source_data)
                    session.add(source)
                    logger.info("Added default skill source: %s", source_data["name"])

            await session.commit()
    except Exception as e:
        logger.warning("Failed to seed default skill sources: %s", e)


async def _start_background_tasks(engine: Any) -> None:
    """Start background tasks after database verification."""
    await _verify_db_connection_and_rls(engine)
    await _ensure_default_skill_sources()

    # Start skill sync scheduler
    try:
        from shandy.skill_scheduler import start_skill_scheduler

        await start_skill_scheduler()
        logger.info("Skill sync scheduler started")
    except Exception as e:
        logger.warning("Failed to start skill sync scheduler: %s", e)


def _initialize_job_manager_runtime(jobs_dir: Path) -> None:
    _state.job_manager = JobManager(
        jobs_dir=jobs_dir,
        max_concurrent=get_settings().max_concurrent_jobs,
    )
    # Add static file serving for job plots
    app.add_static_files("/jobs", str(jobs_dir))
    # Add static file serving for assets (icons, etc.)
    app.add_static_files("/assets", str(ASSETS_DIR))


def init_app(jobs_dir: Path = Path("jobs")) -> None:
    """Initialize the web application."""
    _state.jobs_dir = jobs_dir

    if _state.job_manager is not None:
        logger.info("Web app already initialized, skipping re-initialization")
        return

    try:
        from shandy.database.engine import get_engine

        engine = get_engine()

        async def start_background_tasks_for_engine() -> None:
            await _start_background_tasks(engine)

        # Defer background tasks to NiceGUI's startup event to ensure they run
        # in the correct event loop. Running them before ui.run() causes connections
        # to be bound to a temporary event loop, leading to "Future attached to a
        # different loop" errors when NiceGUI's Uvicorn loop tries to close them.
        app.on_startup(start_background_tasks_for_engine)

    except Exception as e:
        logger.error("Failed to initialize database: %s", e)
        logger.warning("Application will continue but database features may not work")

    _initialize_job_manager_runtime(jobs_dir)

    logger.info("Web app initialized with jobs_dir=%s", jobs_dir)


def main(
    host: str = "0.0.0.0",
    port: int = 8080,
    jobs_dir: Path = Path("jobs"),
) -> None:
    """
    Run the web application.

    Args:
        host: Host to bind to
        port: Port to bind to
        jobs_dir: Directory for jobs
    """
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Log version info at startup
    logger.info("=" * 60)
    logger.info(get_version_string())
    logger.info("=" * 60)

    # Check for configuration errors
    if _settings_error is not None:
        logger.error("Configuration error: %s", _settings_error)
        logger.info("Starting minimal error page to display configuration error in UI")

        # Create the error page and run with minimal config
        _create_config_error_page(_settings_error)

        ui.run(
            host=host,
            port=port,
            title="SHANDY - Server Error",
            favicon=ASSETS_DIR / "favicon.ico",
            reload=False,  # No reload in error mode
            show=False,
            storage_secret=STORAGE_SECRET,
        )
        return  # Exit after running error mode

    logger.info("Settings validated successfully")

    from shandy.settings import get_settings

    reload = get_settings().dev.dev_mode

    # Initialize app BEFORE ui.run() to ensure job_manager is set
    # This must happen before any page is accessed
    init_app(jobs_dir=jobs_dir)

    logger.info("Starting NiceGUI server on %s:%s (reload=%s)", host, port, reload)

    # Run NiceGUI
    ui.run(
        host=host,
        port=port,
        title="SHANDY",
        favicon=ASSETS_DIR / "favicon.ico",
        reload=reload,
        uvicorn_reload_excludes=".nicegui,jobs",  # Don't reload on storage/job changes
        show=False,  # Don't auto-open browser in Docker
        storage_secret=STORAGE_SECRET,  # Required for app.storage.user
    )


if __name__ in {"__main__", "__mp_main__"}:
    parser = argparse.ArgumentParser(description="SHANDY Web Interface")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--jobs-dir", default="jobs", help="Jobs directory")

    # Use parse_known_args so NiceGUI's internally-injected flags (e.g. --reload
    # added by watchfiles in dev mode) don't cause "unrecognised arguments" crashes.
    args, _ = parser.parse_known_args()

    main(host=args.host, port=args.port, jobs_dir=Path(args.jobs_dir))

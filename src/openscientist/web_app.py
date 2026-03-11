"""
NiceGUI web interface for OpenScientist.

Provides web UI for job submission, monitoring, and results viewing.
"""

import argparse
import importlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from nicegui import app, ui

from openscientist.job_manager import JobManager
from openscientist.security import register_scanner_block_middleware
from openscientist.version import get_version_string

# Path to assets directory (favicon, icons, etc.)
ASSETS_DIR = Path(__file__).parent / "assets"
# Path to built-in skills directory at project root
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"
JOBS_DIR_ENV = "OPENSCIENTIST_JOBS_DIR"


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
    from openscientist.settings import get_settings

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
        self.app_configured: bool = False
        self.host_app: FastAPI | None = None


_state = _AppState()


def _register_oauth_routes() -> None:
    """Register OAuth authentication routes with the mounted NiceGUI app."""
    try:
        from openscientist.auth.fastapi_routes import router as auth_router

        app.include_router(auth_router)
        logger.info("OAuth authentication routes registered")
    except Exception as e:
        logger.warning("Failed to register OAuth routes: %s", e)


def _register_api_routes(host_app: FastAPI) -> None:
    """Register REST API routes with the host FastAPI app."""
    try:
        from openscientist.api import api_router

        host_app.include_router(api_router)
        logger.info("REST API routes registered at /api/v1")
    except Exception as e:
        logger.warning("Failed to register API routes: %s", e)


def _register_share_routes() -> None:
    """Register web share routes for session-based job sharing."""
    try:
        from openscientist.webapp_components.share_routes import router as share_router

        app.include_router(share_router)
        logger.info("Share routes registered at /web/shares")
    except Exception as e:
        logger.warning("Failed to register share routes: %s", e)


def _register_review_routes() -> None:
    """Register review token redemption route on the NiceGUI app."""
    try:
        from openscientist.auth.fastapi_routes import redeem_review_token

        app.add_api_route(
            "/review/{token}",
            redeem_review_token,
            methods=["GET"],
            include_in_schema=False,
        )
        logger.info("Review token route registered at /review/{token}")
    except Exception as e:
        logger.warning("Failed to register review routes: %s", e)


# Configure OpenAPI metadata
_APP_TITLE = "OpenScientist API"
_APP_VERSION = "1.0.0"
_APP_DESCRIPTION = "REST API for Scientific Hypothesis Agent for Novel Discovery"


def _register_openapi_docs(host_app: FastAPI) -> None:
    """Register OpenAPI documentation routes (Swagger UI and ReDoc).

    NiceGUI disables FastAPI's built-in docs, so we add them manually.
    """

    @host_app.get("/api-docs", include_in_schema=False)
    async def swagger_ui_html() -> HTMLResponse:
        """Swagger UI documentation."""
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{host_app.title} - Swagger UI",
        )

    @host_app.get("/api-redoc", include_in_schema=False)
    async def redoc_html() -> HTMLResponse:
        """ReDoc documentation."""
        return get_redoc_html(
            openapi_url="/openapi.json",
            title=f"{host_app.title} - ReDoc",
        )

    @host_app.get("/openapi.json", include_in_schema=False)
    async def openapi_json() -> JSONResponse:
        """OpenAPI schema as JSON, filtered to only include /api/v1/* paths."""
        schema = host_app.openapi()
        # Filter to only include API paths
        filtered_paths = {
            path: ops for path, ops in schema.get("paths", {}).items() if path.startswith("/api/")
        }
        filtered_schema = {**schema, "paths": filtered_paths}
        return JSONResponse(filtered_schema)

    logger.info("OpenAPI documentation registered at /api-docs and /api-redoc")


def _register_health_endpoint(host_app: FastAPI) -> None:
    """Register a lightweight health endpoint for container checks."""

    @host_app.get("/health", include_in_schema=False)
    def health_check() -> JSONResponse:
        # Return JSON without touching NiceGUI storage to avoid reload churn.
        return JSONResponse({"status": "ok"})


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

            # Verify openscientist_app role exists and is not superuser
            role_result = await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'openscientist_app'"
                )
            )
            role_row = role_result.first()
            if role_row is None:
                logger.error(
                    "RLS CHECK FAILED: 'openscientist_app' role does not exist — "
                    "run 'alembic upgrade head' to create it"
                )
            elif role_row[0]:
                logger.error(
                    "RLS CHECK FAILED: 'openscientist_app' role is a SUPERUSER — "
                    "this bypasses all RLS policies"
                )
            elif role_row[1]:
                logger.error(
                    "RLS CHECK FAILED: 'openscientist_app' role has BYPASSRLS — "
                    "this bypasses all RLS policies"
                )
            else:
                logger.info("RLS CHECK: openscientist_app role is correctly configured")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        logger.warning("Application will continue but database features may not work")


async def _ensure_default_skill_sources() -> None:
    """Ensure default skill sources exist in the database."""
    from sqlalchemy import select

    from openscientist.database.models import SkillSource
    from openscientist.database.session import get_admin_session

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
            "name": "OpenScientist Built-in Skills",
            "path": str(BUILTIN_SKILLS_DIR),
            "is_enabled": True,
        },
    ]

    try:
        async with get_admin_session() as session:
            for source_data in default_sources:
                stmt = select(SkillSource).where(SkillSource.name == source_data["name"])
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if not existing:
                    source = SkillSource(**source_data)
                    session.add(source)
                    logger.info("Added default skill source: %s", source_data["name"])
                else:
                    new_path = source_data.get("path")
                    if isinstance(new_path, str) and existing.path != new_path:
                        existing.path = new_path

            await session.commit()
    except Exception as e:
        logger.warning("Failed to seed default skill sources: %s", e)


async def _start_background_tasks(engine: Any) -> None:
    """Start background tasks after database verification."""
    await _verify_db_connection_and_rls(engine)
    await _ensure_default_skill_sources()

    # Start skill sync scheduler
    try:
        from openscientist.skill_scheduler import start_skill_scheduler

        await start_skill_scheduler()
        logger.info("Skill sync scheduler started")
    except Exception as e:
        logger.warning("Failed to start skill sync scheduler: %s", e)


def _initialize_job_manager_runtime(jobs_dir: Path) -> None:
    if _state.job_manager is not None:
        return
    _state.job_manager = JobManager(
        jobs_dir=jobs_dir,
        max_concurrent=get_settings().max_concurrent_jobs,
    )


def _register_nicegui_static_files(jobs_dir: Path) -> None:
    """Register static files on the mounted NiceGUI app.

    This preserves existing routing behavior where the `/jobs` UI page
    wins for the exact path, while `/jobs/*` serves generated artifacts.
    """
    for mount, directory in (("/jobs", jobs_dir), ("/assets", ASSETS_DIR)):
        try:
            app.add_static_files(mount, str(directory))
        except (RuntimeError, ValueError) as e:
            logger.debug("Static files already registered for %s: %s", mount, e)


def _jobs_dir_from_env(default: Path = Path("jobs")) -> Path:
    return Path(os.getenv(JOBS_DIR_ENV, str(default)))


def _create_lifespan() -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_host_app: FastAPI) -> AsyncIterator[None]:
        try:
            from openscientist.database.engine import get_engine

            engine = get_engine()
            await _start_background_tasks(engine)
        except Exception as e:
            logger.error("Failed to initialize database: %s", e)
            logger.warning("Application will continue but database features may not work")
        yield

    return lifespan


def _configure_host_app(host_app: FastAPI, jobs_dir: Path) -> None:
    """Configure middleware, routes, and mounted NiceGUI app before startup."""
    if _state.app_configured:
        return

    # Middleware and routes must be registered before startup.
    register_scanner_block_middleware(host_app)
    _register_openapi_docs(host_app)
    _register_health_endpoint(host_app)
    _register_api_routes(host_app)
    _register_oauth_routes()
    _register_share_routes()
    _register_review_routes()

    _initialize_job_manager_runtime(jobs_dir)

    # Import page modules so @ui.page decorators are registered.
    importlib.import_module("openscientist.webapp_components.pages")
    _register_nicegui_static_files(jobs_dir)

    ui.run_with(
        host_app,
        mount_path="/",
        title="OpenScientist",
        favicon=ASSETS_DIR / "favicon.ico",
        storage_secret=STORAGE_SECRET,
    )

    _state.app_configured = True
    logger.info("Web app initialized with jobs_dir=%s", jobs_dir)


def create_app(jobs_dir: Path | None = None) -> FastAPI:
    """Create a host FastAPI app and mount NiceGUI at root."""
    resolved_jobs_dir = Path(jobs_dir) if jobs_dir is not None else _jobs_dir_from_env()

    if _state.host_app is not None:
        if _state.jobs_dir != resolved_jobs_dir:
            logger.warning(
                "create_app called with jobs_dir=%s after initialization; using existing jobs_dir=%s",
                resolved_jobs_dir,
                _state.jobs_dir,
            )
        return _state.host_app

    _state.jobs_dir = resolved_jobs_dir
    host_app = FastAPI(
        title=_APP_TITLE,
        version=_APP_VERSION,
        description=_APP_DESCRIPTION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_create_lifespan(),
    )
    _configure_host_app(host_app, resolved_jobs_dir)
    _state.host_app = host_app
    return host_app


def init_app(jobs_dir: Path = Path("jobs")) -> None:
    """Backward-compatible initialization wrapper."""
    create_app(jobs_dir=jobs_dir)


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
            title="OpenScientist - Server Error",
            favicon=ASSETS_DIR / "favicon.ico",
            reload=False,  # No reload in error mode
            show=False,
            storage_secret=STORAGE_SECRET,
        )
        return  # Exit after running error mode

    logger.info("Settings validated successfully")

    from openscientist.settings import get_settings

    reload = get_settings().dev.dev_mode
    os.environ[JOBS_DIR_ENV] = str(jobs_dir)

    logger.info("Starting NiceGUI server on %s:%s (reload=%s)", host, port, reload)

    if reload:
        # Reload mode requires an import string application target.
        uvicorn.run(
            "openscientist.web_app:create_app",
            host=host,
            port=port,
            reload=True,
            factory=True,
            reload_excludes=[".nicegui", str(jobs_dir.resolve())],
            log_level="warning",
        )
        return

    host_app = create_app(jobs_dir=jobs_dir)
    uvicorn.run(
        host_app,
        host=host,
        port=port,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenScientist Web Interface")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--jobs-dir", default="jobs", help="Jobs directory")

    # Use parse_known_args so reload/watcher-injected flags do not cause
    # "unrecognised arguments" crashes in wrapped launch contexts.
    args, _ = parser.parse_known_args()

    main(host=args.host, port=args.port, jobs_dir=Path(args.jobs_dir))

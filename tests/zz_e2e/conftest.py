"""E2E test fixtures using Playwright and PostgreSQL testcontainers.

This module provides fixtures for end-to-end testing that:
1. Starts a real PostgreSQL database via testcontainers
2. Runs database migrations
3. Starts the NiceGUI web application as a subprocess
4. Provides Playwright browser pages for UI interactions
5. Provides direct database access for verification
"""

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from playwright.sync_api import Page
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

# Import crypto module to generate encryption key for tests
from shandy.database import crypto

# =============================================================================
# Port and Database URL Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def e2e_server_port() -> int:
    """Find a free port for the E2E test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def e2e_postgres_container() -> Generator[PostgresContainer, None, None]:
    """Start a PostgreSQL container for E2E tests.

    Uses postgres:18 for native uuidv7() function support.
    This is a separate container from unit tests to avoid conflicts.
    """
    with PostgresContainer(
        image="postgres:18",
        username="shandy",
        password="shandy_e2e_password",
        dbname="shandy_e2e",
    ) as postgres:
        yield postgres


@pytest.fixture(scope="session")
def e2e_database_url(e2e_postgres_container: PostgresContainer) -> str:
    """Get the async database URL for E2E tests."""
    sync_url: str = e2e_postgres_container.get_connection_url()
    async_url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    return async_url


# =============================================================================
# Server Fixture
# =============================================================================


def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Wait for server to become available."""
    import urllib.error
    import urllib.request

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def e2e_server(
    e2e_server_port: int,
    e2e_database_url: str,
) -> Generator[str, None, None]:
    """Start the web application server as a subprocess.

    This fixture:
    1. Sets up environment variables for the subprocess
    2. Runs alembic migrations
    3. Starts the web application
    4. Waits for it to be ready
    5. Yields the base URL
    6. Cleans up on teardown

    Yields:
        Base URL of the running server (e.g., "http://127.0.0.1:8080")
    """
    project_root = Path(__file__).parent.parent.parent

    # Set up environment for subprocess
    env = os.environ.copy()
    env["DATABASE_URL"] = e2e_database_url

    # Generate encryption key for E2E tests
    env["TOKEN_ENCRYPTION_KEY"] = crypto.generate_key()

    # Enable mock auth for E2E testing
    env["ENABLE_MOCK_AUTH"] = "true"
    env["AUTH_APP_URL"] = f"http://127.0.0.1:{e2e_server_port}"
    env["AUTH_STORAGE_SECRET"] = "e2e-test-storage-secret-key-12345"

    # Use anthropic provider with test key (won't be used in basic E2E tests)
    env["CLAUDE_PROVIDER"] = "anthropic"
    env["ANTHROPIC_API_KEY"] = "test-api-key-for-e2e"

    # Clear pytest-related environment variables so NiceGUI doesn't detect pytest
    # NiceGUI checks for pytest and expects NICEGUI_SCREEN_TEST_PORT
    for key in list(env.keys()):
        if key.startswith("PYTEST") or key == "_PYTEST_RAISE":
            del env[key]

    # Run alembic migrations
    migration_result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
    )
    if migration_result.returncode != 0:
        raise RuntimeError(
            f"Alembic migration failed:\nSTDOUT: {migration_result.stdout}\n"
            f"STDERR: {migration_result.stderr}"
        )

    # Create shandy_app role for RLS testing (mirrors test_engine fixture)
    sync_db_url = e2e_database_url.replace("postgresql+asyncpg://", "postgresql://")
    role_result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            f"""
import psycopg2
conn = psycopg2.connect("{sync_db_url}")
conn.autocommit = True
cur = conn.cursor()
cur.execute('''
    DO $$ BEGIN CREATE ROLE shandy_app NOLOGIN;
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
''')
cur.execute('GRANT ALL ON ALL TABLES IN SCHEMA public TO shandy_app')
cur.execute('GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO shandy_app')
conn.close()
""",
        ],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
    )
    if role_result.returncode != 0:
        # Non-fatal: role creation might fail if psycopg2 isn't installed
        # The tests will still work, just without RLS role enforcement
        pass

    # Start the web application server
    server_process = subprocess.Popen(
        [
            "uv",
            "run",
            "python",
            "-m",
            "shandy.web_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(e2e_server_port),
        ],
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{e2e_server_port}"

    # Wait for server to be ready
    if not _wait_for_server(base_url, timeout=60.0):
        # Server didn't start - try to capture error output
        server_process.kill()
        stdout, stderr = server_process.communicate(timeout=5)
        raise RuntimeError(
            f"Server failed to start within 60 seconds.\n"
            f"STDOUT: {stdout.decode() if stdout else 'N/A'}\n"
            f"STDERR: {stderr.decode() if stderr else 'N/A'}"
        )

    yield base_url

    # Cleanup: terminate server
    server_process.send_signal(signal.SIGTERM)
    try:
        server_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server_process.kill()


# =============================================================================
# Database Fixtures for Verification
# =============================================================================


@pytest.fixture
def e2e_db_engine(e2e_database_url: str) -> Generator[AsyncEngine, None, None]:
    """Create an async database engine for E2E test verification.

    Note: This is synchronous fixture that creates an async engine.
    Use it with pytest-asyncio async test functions.
    """
    engine = create_async_engine(
        e2e_database_url,
        echo=False,
        pool_size=2,
        max_overflow=5,
    )
    yield engine
    # Engine disposal will be handled by event loop


@pytest_asyncio.fixture
async def e2e_db_session(
    e2e_db_engine: AsyncEngine,
) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session for E2E test verification.

    This session has RLS bypassed to allow full database inspection
    for test assertions.
    """
    async with e2e_db_engine.connect() as conn:
        async_session_maker = async_sessionmaker(
            bind=conn,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        async with async_session_maker() as session:
            # Bypass RLS for test verification queries
            await session.execute(text("SELECT set_config('app.bypass_rls', 'true', false)"))
            yield session


# =============================================================================
# Playwright Fixtures
# =============================================================================


@pytest.fixture
def e2e_page(page: Page, e2e_server: str) -> Page:
    """Provide a Playwright page configured for E2E testing.

    The page is pre-configured with:
    - Base URL set to the E2E server
    - Default timeout of 30 seconds

    Args:
        page: Playwright page fixture (from pytest-playwright)
        e2e_server: Base URL of the running server

    Returns:
        Configured Playwright page
    """
    page.set_default_timeout(30000)  # 30 seconds
    page.set_default_navigation_timeout(30000)
    return page


@pytest.fixture
def e2e_base_url(e2e_server: str) -> str:
    """Provide the base URL for E2E tests.

    This is a convenience fixture that just returns the server URL.
    """
    return e2e_server


# =============================================================================
# Authentication Helper Fixtures
# =============================================================================


@pytest.fixture
def e2e_logged_in_page(page: Page, e2e_server: str, request) -> Page:
    """Provide a page with a logged-in test user.

    Uses mock authentication to create and log in a test user.
    Uses Playwright's API context to POST to the mock callback
    and applies the cookies to the browser context.

    Each test gets a unique user to avoid conflicts.

    Args:
        page: Playwright page fixture (from pytest-playwright)
        e2e_server: Base URL of the running server
        request: pytest request fixture for test name

    Returns:
        Playwright page with authenticated session
    """
    import uuid

    # Configure page timeouts
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(30000)

    # Generate unique user for this test to avoid conflicts
    test_id = uuid.uuid4().hex[:8]
    test_email = f"e2e-{test_id}@example.com"
    test_name = f"E2E User {test_id}"

    # Use Playwright's request API to POST to mock callback
    api_context = page.context.request

    # POST to mock callback endpoint
    response = api_context.post(
        f"{e2e_server}/auth/mock/callback",
        form={
            "email": test_email,
            "name": test_name,
            "username": f"e2e{test_id}",
        },
    )

    # Check if login was successful
    # The response URL indicates success or failure
    if "error=" in response.url:
        raise RuntimeError(
            f"Mock login failed.\n"
            f"Response URL: {response.url}\n"
            f"Response body: {response.text()[:500]}"
        )

    # Navigate to home page to verify
    page.goto(f"{e2e_server}/")
    page.wait_for_load_state("networkidle")

    # Check if we ended up on the home page (not login)
    if "/login" in page.url:
        raise RuntimeError(
            f"Login succeeded but session validation failed.\nCurrent URL: {page.url}"
        )

    return page

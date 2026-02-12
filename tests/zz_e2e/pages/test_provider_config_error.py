"""E2E tests for provider configuration error display.

These tests verify that:
1. The config error is NOT visible on the login page (before authentication)
2. The config error IS visible on the jobs page after login
3. The error message content is correct
4. The Technical Details section shows specific errors
5. The New Job button is disabled when there's a config error
"""

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Generator

import pytest
from playwright.sync_api import Page, expect

from shandy.database import crypto

pytestmark = pytest.mark.e2e


# =============================================================================
# Fixtures for Config Error Server
# =============================================================================


@pytest.fixture(scope="module")
def config_error_server_port() -> int:
    """Find a free port for the config error test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def config_error_server(
    config_error_server_port: int,
    e2e_database_url: str,
) -> Generator[str, None, None]:
    """Start a server with simulated provider config error.

    This server has SIMULATE_PROVIDER_ERROR=true, which makes
    check_provider_config() return an error state.
    """
    project_root = Path(__file__).parent.parent.parent.parent

    env = os.environ.copy()
    env["DATABASE_URL"] = e2e_database_url
    env["TOKEN_ENCRYPTION_KEY"] = crypto.generate_key()
    env["ENABLE_MOCK_AUTH"] = "true"
    env["AUTH_APP_URL"] = f"http://127.0.0.1:{config_error_server_port}"
    env["AUTH_STORAGE_SECRET"] = "e2e-config-error-test-secret"
    env["CLAUDE_PROVIDER"] = "anthropic"
    env["ANTHROPIC_API_KEY"] = "test-api-key"

    # Enable simulated provider error
    env["SIMULATE_PROVIDER_ERROR"] = "true"

    # Clear pytest env vars
    for key in list(env.keys()):
        if key.startswith("PYTEST") or key == "_PYTEST_RAISE":
            del env[key]

    # Run alembic migrations (required for auth to work)
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

    # Create shandy_app role for RLS (non-fatal if psycopg2 not installed)
    sync_db_url = e2e_database_url.replace("postgresql+asyncpg://", "postgresql://")
    subprocess.run(
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
        capture_output=True,
        text=True,
    )
    # Role creation is non-fatal - tests will still work without RLS role

    # Start server
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
            str(config_error_server_port),
        ],
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{config_error_server_port}"

    # Wait for server
    import urllib.error
    import urllib.request

    start_time = time.time()
    while time.time() - start_time < 60.0:
        try:
            urllib.request.urlopen(base_url, timeout=1)
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.5)
    else:
        server_process.kill()
        stdout, stderr = server_process.communicate(timeout=5)
        raise RuntimeError(
            f"Config error server failed to start.\n"
            f"STDOUT: {stdout.decode() if stdout else 'N/A'}\n"
            f"STDERR: {stderr.decode() if stderr else 'N/A'}"
        )

    yield base_url

    server_process.send_signal(signal.SIGTERM)
    try:
        server_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server_process.kill()


@pytest.fixture
def config_error_page(page: Page, config_error_server: str) -> Page:
    """Provide a page for config error testing (not logged in)."""
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(30000)
    return page


@pytest.fixture
def config_error_logged_in_page(page: Page, config_error_server: str) -> Page:
    """Provide a logged-in page for config error testing."""
    import uuid

    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(30000)

    test_id = uuid.uuid4().hex[:8]
    api_context = page.context.request

    response = api_context.post(
        f"{config_error_server}/auth/mock/callback",
        form={
            "email": f"config-error-{test_id}@example.com",
            "name": f"Config Error User {test_id}",
            "username": f"configerror{test_id}",
        },
    )

    if "error=" in response.url:
        raise RuntimeError(f"Mock login failed: {response.url}")

    page.goto(f"{config_error_server}/")
    page.wait_for_load_state("networkidle")

    return page


# =============================================================================
# Tests
# =============================================================================


class TestConfigErrorNotVisibleBeforeLogin:
    """Tests that config error is NOT shown before authentication."""

    def test_login_page_does_not_show_config_error(
        self, config_error_page: Page, config_error_server: str
    ):
        """Login page should not reveal config errors to unauthenticated users."""
        config_error_page.goto(f"{config_error_server}/login")

        # Login page should load normally
        expect(config_error_page.locator("text=SHANDY")).to_be_visible()
        expect(config_error_page.locator("button:has-text('Mock Login')")).to_be_visible()

        # Config error should NOT be visible
        expect(config_error_page.locator("text=Server Configuration Error")).not_to_be_visible()
        expect(config_error_page.locator("text=ANTHROPIC")).not_to_be_visible()

    def test_unauthenticated_user_redirected_to_login(
        self, config_error_page: Page, config_error_server: str
    ):
        """Unauthenticated users should be redirected, not see the error."""
        config_error_page.goto(f"{config_error_server}/jobs")

        # Should redirect to login
        expect(config_error_page).to_have_url(f"{config_error_server}/login")

        # Config error should NOT be visible on login page
        expect(config_error_page.locator("text=Server Configuration Error")).not_to_be_visible()


class TestConfigErrorVisibleAfterLogin:
    """Tests that config error IS shown after authentication."""

    def test_config_error_visible_on_jobs_page(
        self, config_error_logged_in_page: Page, config_error_server: str
    ):
        """Config error should be visible on jobs page after login."""
        config_error_logged_in_page.goto(f"{config_error_server}/jobs")

        # Error banner should be visible
        expect(
            config_error_logged_in_page.locator("text=Server Configuration Error")
        ).to_be_visible()

    def test_config_error_message_content(
        self, config_error_logged_in_page: Page, config_error_server: str
    ):
        """Config error should show correct message content."""
        config_error_logged_in_page.goto(f"{config_error_server}/jobs")

        # Check error message content
        expect(
            config_error_logged_in_page.locator(
                "text=The ANTHROPIC provider is not configured correctly"
            )
        ).to_be_visible()
        expect(
            config_error_logged_in_page.locator(
                "text=Jobs cannot be started until this is resolved"
            )
        ).to_be_visible()
        expect(
            config_error_logged_in_page.locator("text=Please contact the system administrator")
        ).to_be_visible()

    def test_new_job_button_disabled(
        self, config_error_logged_in_page: Page, config_error_server: str
    ):
        """New Job button should be disabled when there's a config error."""
        config_error_logged_in_page.goto(f"{config_error_server}/jobs")

        # New Job button should be disabled
        new_job_button = config_error_logged_in_page.locator("button:has-text('New Job')")
        expect(new_job_button).to_be_visible()
        expect(new_job_button).to_be_disabled()

    def test_technical_details_expandable(
        self, config_error_logged_in_page: Page, config_error_server: str
    ):
        """Technical Details section should be expandable."""
        config_error_logged_in_page.goto(f"{config_error_server}/jobs")

        # Technical Details expansion should be visible (Quasar expansion panel)
        tech_details = config_error_logged_in_page.locator("text=Technical Details").first
        expect(tech_details).to_be_visible()

        # Click to expand
        tech_details.click()

        # Wait for expansion animation
        config_error_logged_in_page.wait_for_timeout(500)

        # Check that specific error details are shown
        expect(
            config_error_logged_in_page.locator("text=ANTHROPIC_API_KEY is missing or invalid")
        ).to_be_visible()
        expect(
            config_error_logged_in_page.locator(
                "text=Please contact your administrator to configure API credentials"
            )
        ).to_be_visible()

    def test_other_navigation_still_works(
        self, config_error_logged_in_page: Page, config_error_server: str
    ):
        """Other navigation should still work despite config error."""
        config_error_logged_in_page.goto(f"{config_error_server}/jobs")

        # Billing button should work
        config_error_logged_in_page.click("button:has-text('Billing')")
        config_error_logged_in_page.wait_for_url(f"{config_error_server}/billing")
        expect(config_error_logged_in_page.locator("text=SHANDY - Billing")).to_be_visible()

        # Admin button should work
        config_error_logged_in_page.goto(f"{config_error_server}/jobs")
        config_error_logged_in_page.click("button:has-text('Admin')")
        config_error_logged_in_page.wait_for_url(f"{config_error_server}/admin")
        expect(config_error_logged_in_page.locator("text=SHANDY - Admin")).to_be_visible()

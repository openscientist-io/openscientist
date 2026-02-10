"""Global pytest fixtures for SHANDY tests."""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shandy.database.models import APIKey, Job, User
from shandy.database.rls import bypass_rls


def _apply_alembic_migrations(database_url: str) -> None:
    """Apply all Alembic migrations to the test database.

    This ensures RLS policies and other migrations are properly applied.
    Uses subprocess for complete isolation from async event loop.

    Args:
        database_url: Async database URL (postgresql+asyncpg://...)

    Raises:
        RuntimeError: If migrations fail to apply
    """
    # Set environment variable for Alembic
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url

    # Get the path to the project root (where alembic.ini is)
    project_root = Path(__file__).parent.parent

    # Check if alembic.ini exists
    alembic_ini = project_root / "alembic.ini"
    if not alembic_ini.exists():
        raise RuntimeError(
            f"alembic.ini not found at {alembic_ini}. Cannot apply database migrations."
        )

    # Run alembic upgrade via subprocess
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_msg = (
            f"Alembic migration failed with exit code {result.returncode}.\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}\n\n"
            f"Common causes:\n"
            f"1. Database not accessible at {database_url}\n"
            f"2. Migration file syntax errors\n"
            f"3. Database permissions issues\n"
            f"4. Migration dependencies not met"
        )
        raise RuntimeError(error_msg)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Get test database URL from environment or use default.

    Priority:
    1. TEST_DATABASE_URL if explicitly set
    2. DATABASE_URL from .env, with hostname replacement for Docker
    3. Default localhost:5434 connection
    """
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        return test_url

    # Get DATABASE_URL and replace Docker hostname with localhost for testing
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        # Replace Docker hostname with localhost and port 5434
        db_url = db_url.replace("@postgres:5432", "@localhost:5434")
        return db_url

    # Final fallback
    return "postgresql+asyncpg://shandy:shandy_dev_password@localhost:5434/shandy"


@pytest_asyncio.fixture
async def test_engine(test_database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """Create a test database engine with migrations applied.

    Note: Changed from session-scoped to function-scoped to avoid event loop conflicts.
    Each test gets a fresh database with all tables and RLS policies applied.

    Raises:
        RuntimeError: If database connection fails or is not available
    """
    engine = create_async_engine(
        test_database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
    )

    # Verify database connection is available
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        await engine.dispose()
        raise RuntimeError(
            f"Cannot connect to test database at {test_database_url}. "
            f"Please ensure PostgreSQL is running and accessible. "
            f"Error: {type(e).__name__}: {e}"
        ) from e

    # Clean database before test to ensure fresh state
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO PUBLIC"))
    except Exception as e:
        await engine.dispose()
        raise RuntimeError(
            f"Failed to clean test database schema. Error: {type(e).__name__}: {e}"
        ) from e

    # Apply Alembic migrations (which creates tables AND adds RLS policies)
    try:
        _apply_alembic_migrations(test_database_url)
    except Exception as e:
        await engine.dispose()
        raise RuntimeError(
            f"Failed to apply Alembic migrations. "
            f"This may indicate migration syntax errors or database issues. "
            f"Error: {e}"
        ) from e

    # Create a non-superuser app role for RLS enforcement.
    # Superusers always bypass RLS, so tests must use a regular role.
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE shandy_app NOLOGIN; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO shandy_app"))
            await conn.execute(text("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO shandy_app"))
    except Exception as e:
        await engine.dispose()
        raise RuntimeError(
            f"Failed to create app role for RLS testing. Error: {type(e).__name__}: {e}"
        ) from e

    yield engine

    # Drop all tables after test with CASCADE to handle RLS policy dependencies
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO PUBLIC"))
    except Exception:
        # Ignore cleanup errors - test database will be cleaned on next run
        pass

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session for a test.

    Uses a persistent connection to ensure RLS settings persist across queries.
    All operations within the test use the same database connection.
    """
    # Get a persistent connection for the test
    async with test_engine.connect() as conn:
        # Create session bound to this connection
        async_session_maker = async_sessionmaker(
            bind=conn,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        async with async_session_maker() as session:
            # Switch to non-superuser role so RLS policies are enforced.
            # Superusers always bypass RLS, even with FORCE ROW LEVEL SECURITY.
            await session.execute(text("SET ROLE shandy_app"))

            # Clear RLS context for test isolation (session-local, persists across transactions)
            await session.execute(text("SELECT set_config('app.current_user_id', NULL, false)"))
            await session.execute(text("SELECT set_config('app.bypass_rls', '', false)"))

            yield session


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> User:
    """Create a test user."""
    async with bypass_rls(db_session):
        user = User(
            email="test@example.com",
            name="Test User",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_user2(db_session: AsyncSession) -> User:
    """Create a second test user."""
    async with bypass_rls(db_session):
        user = User(
            email="test2@example.com",
            name="Test User 2",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_job(db_session: AsyncSession, test_user: User) -> Job:
    """Create a test job owned by test_user."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=test_user.id,
            title="Test Job",
            description="A test job for testing",
            llm_provider="mock",
            llm_config={"model": "mock-model-v1"},
            status="pending",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
    return job


@pytest_asyncio.fixture
async def test_api_key(db_session: AsyncSession, test_user: User) -> APIKey:
    """Create a test API key for test_user."""
    async with bypass_rls(db_session):
        api_key = APIKey(
            user_id=test_user.id,
            name="Test API Key",
            key_hash="test_hash_12345",
        )
        db_session.add(api_key)
        await db_session.commit()
        await db_session.refresh(api_key)
    return api_key


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_jobs_dir(temp_dir: Path) -> Path:
    """Create a temporary jobs directory structure."""
    jobs_dir = temp_dir / "jobs"
    jobs_dir.mkdir()
    return jobs_dir


@pytest.fixture
def sample_job_config() -> dict:
    """Sample job configuration for testing."""
    return {
        "job_id": "test_job_123",
        "research_question": "Test research question",
        "provider": "mock",
        "model": "mock-model",
        "coinvestigate": False,
        "status": "pending",
        "created_at": "2026-02-05T10:00:00",
    }


@pytest.fixture
def sample_knowledge_state() -> dict:
    """Sample knowledge state for testing."""
    return {
        "research_question": "Test research question",
        "iterations": [],
        "current_iteration": 0,
        "status": "pending",
        "plots": [],
        "literature": [],
        "datasets": [],
    }

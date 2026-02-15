"""Global pytest fixtures for SHANDY tests."""

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Generator

# Set up test environment variables BEFORE any shandy imports
# This prevents settings validation errors during test collection
if "CLAUDE_PROVIDER" not in os.environ or os.environ.get("CLAUDE_PROVIDER") == "cborg":
    os.environ["CLAUDE_PROVIDER"] = "anthropic"
if "ANTHROPIC_API_KEY" not in os.environ:
    os.environ["ANTHROPIC_API_KEY"] = "test-api-key-for-testing"

import pytest
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

# Re-export enable_rls from helpers for backward compatibility
from tests.helpers import enable_rls as enable_rls  # noqa: F401


@pytest.fixture
def event_loop():
    """Create a new event loop for each test.

    This overrides pytest-playwright's event_loop fixture to prevent
    conflicts with async database tests and NiceGUI user_simulation.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# Clear any cached settings after environment setup
# This must be done after shandy imports to access the cache_clear function
def _clear_settings_cache():
    """Clear settings cache - called after imports."""
    try:
        from shandy.settings import clear_settings_cache

        clear_settings_cache()
    except ImportError:
        pass


import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shandy.database.models import (  # noqa: E402
    Administrator,
    APIKey,
    Job,
    Skill,
    SkillSource,
    User,
)

# Set up encryption key for tests (required for EncryptedText columns)
# This must be done before any database operations that use encrypted fields
if "TOKEN_ENCRYPTION_KEY" not in os.environ:
    from shandy.database import crypto

    os.environ["TOKEN_ENCRYPTION_KEY"] = crypto.generate_key()
    # Clear the cached Fernet instance so it picks up the new key
    crypto._get_fernet.cache_clear()

# Clear and reload settings cache after setting up test environment
_clear_settings_cache()


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    """Start a PostgreSQL container for the test session.

    This container is shared across all tests in the session for efficiency.
    It's only started if TEST_DATABASE_URL is not explicitly set.

    Uses postgres:18 for native uuidv7() function support.
    """
    # Skip container if explicit database URL is provided
    if os.getenv("TEST_DATABASE_URL"):
        yield None  # type: ignore[misc]
        return

    # Start PostgreSQL 18 container (required for native uuidv7() support)
    # Uses same credentials as dev environment
    with PostgresContainer(
        image="postgres:18",
        username="shandy",
        password="shandy_dev_password",
        dbname="shandy",
    ) as postgres:
        yield postgres


@pytest.fixture(scope="session")
def _apply_migrations_once(test_database_url: str) -> None:
    """Apply migrations once at session start (synchronous).

    This fixture applies Alembic migrations once per test session rather than
    per test, significantly improving test performance. Individual tests use
    transaction rollback for isolation instead of schema recreation.

    Uses asyncio.run() to execute async database operations since only asyncpg
    driver is available.
    """

    async def _setup_schema() -> None:
        engine = create_async_engine(test_database_url, echo=False)

        # Clean schema before applying migrations
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO PUBLIC"))

        await engine.dispose()

    async def _setup_roles() -> None:
        engine = create_async_engine(test_database_url, echo=False)

        # Create roles to match production environment:
        # - shandy_app: Non-superuser role subject to RLS (used by get_session)
        # - shandy_admin: Role with BYPASSRLS (used by get_admin_session)
        async with engine.begin() as conn:
            # App role - subject to RLS
            await conn.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE shandy_app NOLOGIN; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO shandy_app"))
            await conn.execute(text("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO shandy_app"))

            # Admin role - bypasses RLS (matches docker/postgres/init.sql)
            await conn.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE shandy_admin NOLOGIN BYPASSRLS; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO shandy_admin"))
            await conn.execute(text("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO shandy_admin"))

        await engine.dispose()

    # Run schema setup
    asyncio.run(_setup_schema())

    # Apply Alembic migrations (creates tables AND adds RLS policies)
    _apply_alembic_migrations(test_database_url)

    # Create app and admin roles
    asyncio.run(_setup_roles())


@pytest.fixture
def clear_settings():
    """Fixture to clear settings cache before and after test.

    Use this fixture in tests that need to patch environment variables
    and have the settings reload with new values.
    """
    from shandy.settings import clear_settings_cache

    clear_settings_cache()
    yield
    clear_settings_cache()


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
def test_database_url(postgres_container: PostgresContainer | None) -> str:
    """Get test database URL from environment or testcontainer.

    Priority:
    1. TEST_DATABASE_URL if explicitly set
    2. Testcontainer PostgreSQL (auto-started)
    """
    # Check for explicit test database URL
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        return test_url

    # Use testcontainer - it should always be available at this point
    if postgres_container is None:
        raise RuntimeError(
            "No TEST_DATABASE_URL set and postgres_container fixture returned None. "
            "This should not happen - check the postgres_container fixture."
        )

    # Get connection URL from container and convert to asyncpg driver
    # testcontainers returns psycopg2 URL, we need asyncpg
    sync_url: str = postgres_container.get_connection_url()
    async_url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    return async_url


@pytest_asyncio.fixture
async def test_engine(
    test_database_url: str,
    _apply_migrations_once: None,  # Depends on session-scoped migration
) -> AsyncGenerator[AsyncEngine, None]:
    """Create a test database engine (migrations already applied at session start).

    Function-scoped to avoid event loop conflicts. Schema and migrations are
    applied once per session by the _apply_migrations_once fixture.

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

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session with RLS enforced (matches production behavior).

    Uses the transaction rollback pattern: each test runs inside a transaction
    that is rolled back at the end, restoring the database to its initial state.

    The session runs as shandy_admin role which has BYPASSRLS privilege.
    This matches how get_admin_session() works in production.

    For most tests, this is the right fixture to use. Tests can then call
    set_current_user() to simulate a specific user's view of the data.
    """
    async with test_engine.connect() as conn:
        # Start outer transaction (will be rolled back at end)
        trans = await conn.begin()

        async_session_maker = async_sessionmaker(
            bind=conn,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        async with async_session_maker() as session:
            # Run as shandy_admin (BYPASSRLS) - matches get_admin_session()
            # This allows fixtures to create data across tenants
            await session.execute(text("SET ROLE shandy_admin"))

            # Clear RLS context for test isolation
            await session.execute(text("SELECT set_config('app.current_user_id', NULL, false)"))

            yield session

            await session.execute(text("RESET ROLE"))

        # Rollback outer transaction - undoes ALL changes from this test
        await trans.rollback()


@pytest_asyncio.fixture
async def db_session_rls(
    test_engine: AsyncEngine,
) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session with RLS enforced (for testing RLS behavior).

    Uses shandy_app role which is subject to RLS policies.
    Use this fixture for tests that specifically verify RLS blocks access.

    Remember to call set_current_user() to set which user's view to test.
    """
    async with test_engine.connect() as conn:
        trans = await conn.begin()

        async_session_maker = async_sessionmaker(
            bind=conn,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        async with async_session_maker() as session:
            # Run as shandy_app (subject to RLS) - matches get_session()
            await session.execute(text("SET ROLE shandy_app"))

            # Clear RLS context
            await session.execute(text("SELECT set_config('app.current_user_id', NULL, false)"))

            yield session

            await session.execute(text("RESET ROLE"))

        await trans.rollback()


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> User:
    """Create a test user."""
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
    user = User(
        email="test2@example.com",
        name="Test User 2",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_admin_user(db_session: AsyncSession) -> User:
    """Create an admin test user."""
    user = User(
        email="admin@example.com",
        name="Admin User",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    # Grant admin privileges
    admin = Administrator(user_id=user.id)
    db_session.add(admin)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def test_job(db_session: AsyncSession, test_user: User) -> Job:
    """Create a test job owned by test_user."""
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


# =============================================================================
# Skill Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def test_skill_source(db_session: AsyncSession) -> SkillSource:
    """Create a test skill source."""
    source = SkillSource(
        name="Test Skills",
        source_type="github",
        url="https://github.com/test/test-skills",
        branch="main",
        skills_path="skills",
        is_enabled=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest_asyncio.fixture
async def test_skill(db_session: AsyncSession, test_skill_source: SkillSource) -> Skill:
    """Create a test skill."""
    skill = Skill(
        name="Metabolomics Analysis",
        slug="metabolomics-analysis",
        category="metabolomics",
        description="Statistical analysis of metabolomics data",
        content="# Metabolomics Analysis\n\nThis skill provides guidance...",
        tags=["statistics", "metabolomics", "analysis"],
        source_id=test_skill_source.id,
        source_path="metabolomics/analysis.md",
        content_hash="abc123def456",
        is_enabled=True,
    )
    db_session.add(skill)
    await db_session.commit()

    # Manually populate search_vector since trigger may not run in tests
    await db_session.execute(
        text(
            """
            UPDATE skills SET search_vector =
                setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(description, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(category, '')), 'C')
            WHERE id = :skill_id
        """
        ),
        {"skill_id": skill.id},
    )
    await db_session.commit()
    await db_session.refresh(skill)
    return skill


@pytest_asyncio.fixture
async def test_skill2(db_session: AsyncSession, test_skill_source: SkillSource) -> Skill:
    """Create a second test skill."""
    skill = Skill(
        name="Genomics Pipeline",
        slug="genomics-pipeline",
        category="genomics",
        description="Genomics data processing pipeline",
        content="# Genomics Pipeline\n\nThis skill provides guidance...",
        tags=["genomics", "pipeline", "bioinformatics"],
        source_id=test_skill_source.id,
        source_path="genomics/pipeline.md",
        content_hash="xyz789ghi012",
        is_enabled=True,
    )
    db_session.add(skill)
    await db_session.commit()

    # Manually populate search_vector since trigger may not run in tests
    await db_session.execute(
        text(
            """
            UPDATE skills SET search_vector =
                setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(description, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(category, '')), 'C')
            WHERE id = :skill_id
        """
        ),
        {"skill_id": skill.id},
    )
    await db_session.commit()
    await db_session.refresh(skill)
    return skill


@pytest.fixture
def sample_skill_markdown() -> str:
    """Sample skill markdown content with YAML frontmatter."""
    return """---
name: Test Skill
category: testing
description: A skill for testing purposes
tags:
  - test
  - example
---

# Test Skill

This is the skill content.

## Usage

Use this skill when testing.
"""

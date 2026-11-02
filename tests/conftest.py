"""Global pytest fixtures for OpenScientist tests."""

import asyncio
import os
import subprocess
import tempfile
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from functools import partial
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from openscientist.database.models import (
    Administrator,
    APIKey,
    Job,
    Skill,
    SkillSource,
    User,
)
from openscientist.exec_broker_client import BudgetCheck
from openscientist.orchestrator import discovery
from tests.admin_row import reset_admin_settings_row
from tests.db_logins import (
    ADMIN_PASSWORD,
    BOOTSTRAP_ENV,
    BOOTSTRAP_SQL,
    CONTAINER_BOOTSTRAP_DIR,
    OWNER_PASSWORD,
    WEB_PASSWORD,
    login_url,
    reset_database_state,
    run_bootstrap,
)
from tests.db_outage import Forwarder


# Clear any cached settings after environment setup
# This must be done after openscientist imports to access the cache_clear function
def _clear_settings_cache():
    """Clear settings cache - called after imports."""
    try:
        from openscientist.settings import clear_settings_cache

        clear_settings_cache()
    except ImportError:
        pass


# Set up required env vars for Settings validation at import time.
# The real DATABASE_URL comes from the testcontainer later.
if "OPENSCIENTIST_SECRET_KEY" not in os.environ:
    os.environ["OPENSCIENTIST_SECRET_KEY"] = "test-secret-key-for-pytest-do-not-use-in-production"
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://test:test@localhost:5432/test"
# There is no default provider, so tests that do not select one explicitly need a
# valid OPENSCIENTIST_PROVIDER so Settings construction does not raise.
if "OPENSCIENTIST_PROVIDER" not in os.environ:
    os.environ["OPENSCIENTIST_PROVIDER"] = "anthropic"
# Pin the model context window so building report prompts never makes a live
# Ollama probe during tests (the override path short-circuits Provider.model_profile),
# keeping the suite hermetic and deterministic.
if "OPENSCIENTIST_MODEL_CONTEXT_TOKENS" not in os.environ:
    os.environ["OPENSCIENTIST_MODEL_CONTEXT_TOKENS"] = "131072"
# openscientist_tools binds STATE at import, so give it dummy job values.
if "OPENSCIENTIST_JOB_ID" not in os.environ:
    os.environ["OPENSCIENTIST_JOB_ID"] = "test-job"
if "OPENSCIENTIST_JOB_DIR" not in os.environ:
    os.environ["OPENSCIENTIST_JOB_DIR"] = "/tmp/openscientist-test-job"

# Clear and reload settings cache after setting up test environment
_clear_settings_cache()


@pytest.fixture
def event_loop():
    """Create a new event loop for each test.

    This overrides pytest-playwright's event_loop fixture to prevent
    conflicts with async database tests and NiceGUI user_simulation.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def discovery_budget_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop's budget check allows by default. The budget tests override it."""

    async def _allow(*, job_id: str) -> BudgetCheck:
        return BudgetCheck(True, None, None)

    monkeypatch.setattr(discovery, "ask_budget", _allow)


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
        username="openscientist",
        password="openscientist_dev_password",
        dbname="openscientist",
    ).with_volume_mapping(str(BOOTSTRAP_SQL.parent), CONTAINER_BOOTSTRAP_DIR, "ro") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def bootstrap(
    test_database_url: str, postgres_container: PostgresContainer | None
) -> Callable[[Mapping[str, str]], tuple[int, str]]:
    """Run ``docker/postgres/bootstrap.sql`` with the given passwords."""
    return partial(run_bootstrap, test_database_url, postgres_container)


@pytest.fixture(scope="session")
def _apply_migrations_once(
    test_database_url: str, bootstrap: Callable[[Mapping[str, str]], tuple[int, str]]
) -> None:
    """Apply migrations once at session start (synchronous).

    This fixture applies Alembic migrations once per test session rather than
    per test, significantly improving test performance. Individual tests use
    transaction rollback for isolation instead of schema recreation.

    Uses asyncio.run() to execute async database operations since only asyncpg
    driver is available.
    """

    async def _setup_schema() -> None:
        engine = create_async_engine(test_database_url, echo=False)

        # Clean schema before applying migrations. The schema is recreated the
        # way initdb leaves it, owned by the database owner.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public AUTHORIZATION pg_database_owner"))
            await conn.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))

        await engine.dispose()

    asyncio.run(_setup_schema())

    exit_code, output = bootstrap(BOOTSTRAP_ENV)
    if exit_code != 0:
        raise RuntimeError(f"bootstrap.sql failed with exit code {exit_code}:\n{output}")

    _apply_alembic_migrations(login_url(test_database_url, "openscientist_owner", OWNER_PASSWORD))

    # A running deployment seeds the admin settings row at startup, so the
    # session starts the same way and holds the default row between tests.
    async def _seed_admin_row() -> None:
        engine = create_async_engine(test_database_url, echo=False)
        try:
            await reset_admin_settings_row(engine)
        finally:
            await engine.dispose()

    asyncio.run(_seed_admin_row())


def _apply_alembic_migrations(owner_database_url: str) -> None:
    """Apply all Alembic migrations to the test database.

    This ensures RLS policies and other migrations are properly applied.
    Uses subprocess for complete isolation from async event loop.

    Args:
        owner_database_url: Async URL of the openscientist_owner login

    Raises:
        RuntimeError: If migrations fail to apply
    """
    # The variables win over a developer's .env.db, which the settings also read.
    env = os.environ.copy()
    env["OWNER_DATABASE_URL"] = owner_database_url
    env["POSTGRES_OWNER_PASSWORD"] = OWNER_PASSWORD
    env["POSTGRES_ADMIN_PASSWORD"] = ADMIN_PASSWORD

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
        check=False,
    )

    if result.returncode != 0:
        error_msg = (
            f"Alembic migration failed with exit code {result.returncode}.\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}\n\n"
            f"Common causes:\n"
            f"1. Database not accessible at {owner_database_url}\n"
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

    Also propagates the real URL to env/settings so that the app's own sessions
    connect to the testcontainer DB instead of the dummy placeholder URL set at
    import time.
    """
    # Check for explicit test database URL
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        url = test_url
    elif postgres_container is None:
        raise RuntimeError(
            "No TEST_DATABASE_URL set and postgres_container fixture returned None. "
            "This should not happen - check the postgres_container fixture."
        )
    else:
        # Get connection URL from container and convert to asyncpg driver
        # testcontainers returns psycopg2 URL, we need asyncpg
        sync_url: str = postgres_container.get_connection_url()
        url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")

    # Propagate the real URL to the app's sessions
    os.environ["DATABASE_URL"] = url
    os.environ["ADMIN_DATABASE_URL"] = login_url(url, "openscientist_admin", ADMIN_PASSWORD)
    from openscientist.settings import clear_settings_cache

    clear_settings_cache()
    return url


@pytest.fixture
def web_login(test_database_url: str, _apply_migrations_once: None) -> Generator[None, None, None]:
    """Point ``DATABASE_URL`` and ``ADMIN_DATABASE_URL`` at the web's real logins."""
    saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "ADMIN_DATABASE_URL")}
    os.environ["DATABASE_URL"] = login_url(test_database_url, "openscientist_web", WEB_PASSWORD)
    os.environ["ADMIN_DATABASE_URL"] = login_url(
        test_database_url, "openscientist_admin", ADMIN_PASSWORD
    )
    reset_database_state()
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    reset_database_state()


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


@pytest.fixture
def database(test_engine: AsyncEngine) -> Generator[Forwarder, None, None]:
    """Point every app login at a forwarder to the test database, which starts
    up. ``database.down()`` refuses connects until ``database.up()``."""
    names = ("DATABASE_URL", "ADMIN_DATABASE_URL")
    saved = {name: os.environ[name] for name in names}
    target = make_url(saved["DATABASE_URL"])
    forwarder = Forwarder((target.host or "127.0.0.1", target.port or 5432))
    for name in names:
        url = make_url(saved[name]).set(host="127.0.0.1", port=forwarder.port)
        os.environ[name] = url.render_as_string(hide_password=False)
    reset_database_state()
    yield forwarder
    forwarder.down()
    os.environ.update(saved)
    reset_database_state()


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session with RLS enforced (matches production behavior).

    Uses the transaction rollback pattern: each test runs inside a transaction
    that is rolled back at the end, restoring the database to its initial state.

    The session runs as openscientist_admin role which has BYPASSRLS privilege.
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
            # Run as openscientist_admin (BYPASSRLS) - matches get_admin_session()
            # This allows fixtures to create data across tenants
            await session.execute(text("SET ROLE openscientist_admin"))

            # Clear RLS context for test isolation
            await session.execute(text("SELECT set_config('app.current_user_id', NULL, false)"))

            yield session

            await session.execute(text("RESET ROLE"))

        # Rollback outer transaction - undoes ALL changes from this test
        # Some tests intentionally trigger rollback paths that can deassociate
        # the outer transaction before fixture teardown.
        if trans.is_active:
            await trans.rollback()


@pytest_asyncio.fixture
async def db_session_rls(
    test_engine: AsyncEngine,
) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session with RLS enforced (for testing RLS behavior).

    Uses openscientist_app role which is subject to RLS policies.
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
            # Run as openscientist_app (subject to RLS) - matches get_session()
            await session.execute(text("SET ROLE openscientist_app"))

            # Clear RLS context
            await session.execute(text("SELECT set_config('app.current_user_id', NULL, false)"))

            yield session

            await session.execute(text("RESET ROLE"))

        if trans.is_active:
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
        research_question="Test Job",
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

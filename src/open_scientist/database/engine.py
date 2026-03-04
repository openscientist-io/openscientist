"""
Database engine configuration for Open Scientist.

Provides async SQLAlchemy engine instances with PostgreSQL connection pooling.

Engine Types:
    - app_engine: Standard connection with limited privileges, subject to RLS policies
    - admin_engine: Elevated connection that bypasses RLS (via PostgreSQL role)

For the dual-engine pattern to work properly, ADMIN_DATABASE_URL should connect
with a PostgreSQL role that has elevated privileges (table owner or role with
BYPASSRLS). If not configured, falls back to the regular DATABASE_URL.
"""

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from open_scientist.settings import get_settings

# Load environment variables from .env file
load_dotenv()

# Global engine instances
_engine: AsyncEngine | None = None
_admin_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """
    Get or create the async database engine for standard operations.

    This engine uses the app database role with limited privileges.
    All queries through this engine are subject to RLS policies.

    Returns:
        AsyncEngine: The SQLAlchemy async engine instance.

    Raises:
        ValueError: If database is not properly configured.
    """
    global _engine

    if _engine is None:
        settings = get_settings()
        database_url = settings.database.effective_database_url

        _engine = create_async_engine(
            database_url,
            # Connection pool settings
            pool_size=20,  # Maximum number of permanent connections
            max_overflow=10,  # Maximum number of temporary overflow connections
            pool_timeout=30,  # Seconds to wait before giving up on getting a connection
            pool_recycle=3600,  # Recycle connections after 1 hour
            pool_pre_ping=True,  # Verify connections before using them
            # Echo SQL queries in development
            echo=settings.database.sql_echo,
        )

    return _engine


def get_admin_engine() -> AsyncEngine:
    """
    Get or create the admin database engine for privileged operations.

    This engine uses the admin database role with elevated privileges.
    Queries through this engine bypass RLS policies via the PostgreSQL
    role's privileges (table owner or BYPASSRLS grant).

    Use this engine only for:
    - Background schedulers (no user context available)
    - Admin-authenticated endpoints (verified by @require_admin)
    - Migrations and schema changes
    - Test fixtures that need to create data across tenants

    Returns:
        AsyncEngine: The admin SQLAlchemy async engine instance.

    Raises:
        ValueError: If database is not properly configured.
    """
    global _admin_engine

    if _admin_engine is None:
        settings = get_settings()
        database_url = settings.database.effective_admin_database_url

        _admin_engine = create_async_engine(
            database_url,
            # Use NullPool to avoid event loop conflicts in web frameworks
            # Each connection is fresh, avoiding pool-related async issues
            poolclass=NullPool,
            echo=settings.database.sql_echo,
        )

    return _admin_engine

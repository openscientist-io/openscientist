"""
Database engine configuration for SHANDY.

Provides async SQLAlchemy engine instance with PostgreSQL connection pooling.
"""

from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shandy.settings import get_settings

# Load environment variables from .env file
load_dotenv()

# Global engine instance
_engine: Optional[AsyncEngine] = None


def get_engine() -> AsyncEngine:
    """
    Get or create the async database engine.

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


def get_async_engine() -> AsyncEngine:
    """Alias for get_engine() used by migration scripts."""
    return get_engine()

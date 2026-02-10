"""
Database engine configuration for SHANDY.

Provides async SQLAlchemy engine instance with PostgreSQL connection pooling.
"""

import os
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
        ValueError: If DATABASE_URL environment variable is not set.
    """
    global _engine

    if _engine is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError(
                "DATABASE_URL environment variable is not set.\n\n"
                "To fix this:\n"
                "1. Copy .env.example to .env: cp .env.example .env\n"
                "2. Uncomment and configure DATABASE_URL in .env\n"
                "   For local development: DATABASE_URL=postgresql+asyncpg://shandy:shandy_dev_password@localhost:5434/shandy\n"
                "3. Make sure PostgreSQL is running (use 'make dev-start' for Docker setup)\n\n"
                "See CONTRIBUTING.md for complete setup instructions."
            )

        _engine = create_async_engine(
            database_url,
            # Connection pool settings
            pool_size=20,  # Maximum number of permanent connections
            max_overflow=10,  # Maximum number of temporary overflow connections
            pool_timeout=30,  # Seconds to wait before giving up on getting a connection
            pool_recycle=3600,  # Recycle connections after 1 hour
            pool_pre_ping=True,  # Verify connections before using them
            # Echo SQL queries in development (set via env var)
            echo=os.getenv("SQL_ECHO", "false").lower() == "true",
        )

    return _engine


# Create default engine instance
engine = get_engine()

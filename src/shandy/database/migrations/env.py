"""
Alembic migration environment for SHANDY.

This module configures the Alembic migration context to work with async SQLAlchemy
and automatically discover all ORM models for migration generation.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context  # type: ignore[import-untyped]
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import the Base class and all models so Alembic can detect them
from shandy.database.base import Base

# Import all models here so they're registered with Base.metadata
# This is necessary for auto-generating migrations
from shandy.database.models import (  # noqa: F401
    Administrator,
    AnalysisLog,
    APIKey,
    CostRecord,
    FeedbackHistory,
    Finding,
    Hypothesis,
    HypothesisSpawn,
    IterationSummary,
    Job,
    JobChatMessage,
    JobDataFile,
    JobShare,
    Literature,
    MCPServer,
    OAuthAccount,
    Plot,
    Session,
    Skill,
    SkillSource,
    User,
    finding_hypotheses,
    finding_literature,
)

# Alembic Config object provides access to alembic.ini values
config = context.config

# Interpret the config file for Python logging
# This line sets up loggers basically
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set the SQLAlchemy URL from environment variable
# Note: Alembic migrations must read DATABASE_URL directly from environment
# because settings module may not be importable during migrations
database_url: str = os.getenv("DATABASE_URL", "")  # noqa: env-ok
if not database_url:
    raise ValueError(
        "DATABASE_URL environment variable is required for migrations. "
        "Example: postgresql+asyncpg://user:pass@localhost:5432/shandy"
    )

# Set the sqlalchemy.url in the Alembic config
config.set_main_option("sqlalchemy.url", database_url)

# Add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # Detect column type changes
        compare_server_default=True,  # Detect default value changes
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Execute migrations within a database connection.

    Args:
        connection: Active database connection.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # Detect column type changes
        compare_server_default=True,  # Detect default value changes
        # Include schemas if using multi-schema setup
        # include_schemas=True,
        # version_table_schema=target_metadata.schema,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations in 'online' mode with async engine.

    In this scenario we need to create an Engine and associate a connection
    with the context. This is async-aware and uses asyncpg driver.
    """
    # Get Alembic config section for SQLAlchemy settings
    configuration = config.get_section(config.config_ini_section) or {}

    # Override the connection URL
    configuration["sqlalchemy.url"] = database_url

    # Create async engine
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # Don't use connection pooling for migrations
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    Creates an async engine and runs migrations within an async context.
    """
    asyncio.run(run_async_migrations())


# Determine which mode to run migrations in
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

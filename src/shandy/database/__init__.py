"""
Database package for SHANDY.

Provides SQLAlchemy ORM models, session management, and database utilities.

Dual-Engine Pattern:
    - get_session(): Standard session with RLS enforced by PostgreSQL role
    - get_admin_session(): Admin session that bypasses RLS via BYPASSRLS role

For production deployments, configure ADMIN_DATABASE_URL with a PostgreSQL
role that has the BYPASSRLS privilege (created by docker/postgres/init.sql).
"""

from .base import Base, UUIDv7Mixin
from .engine import get_admin_engine, get_engine
from .rls import (
    get_current_user,
    list_rls_policies,
    session_with_user,
    set_current_user,
    verify_rls_enabled,
)
from .session import AsyncSessionLocal, get_admin_session, get_session, get_session_ctx

__all__ = [
    "Base",
    "UUIDv7Mixin",
    # Engines
    "get_engine",
    "get_admin_engine",
    # Sessions
    "get_session",
    "get_session_ctx",
    "get_admin_session",
    "AsyncSessionLocal",
    # RLS utilities
    "set_current_user",
    "get_current_user",
    "session_with_user",
    "verify_rls_enabled",
    "list_rls_policies",
]

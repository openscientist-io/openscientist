"""
Database package for SHANDY.

Provides SQLAlchemy ORM models, session management, and database utilities.
"""

from .base import Base, UUIDv7Mixin
from .engine import engine, get_engine
from .rls import (
    bypass_rls,
    get_current_user,
    list_rls_policies,
    session_with_user,
    set_current_user,
    verify_rls_enabled,
)
from .session import AsyncSessionLocal, get_session

__all__ = [
    "Base",
    "UUIDv7Mixin",
    "engine",
    "get_engine",
    "get_session",
    "AsyncSessionLocal",
    # RLS utilities
    "set_current_user",
    "get_current_user",
    "bypass_rls",
    "session_with_user",
    "verify_rls_enabled",
    "list_rls_policies",
]

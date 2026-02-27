"""Shared test utilities.

This module contains helper functions used across multiple test files.
These are separated from conftest.py so they can be imported without
mypy resolution issues.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def enable_rls(session: AsyncSession) -> None:
    """Switch session from admin role to app role (enables RLS enforcement).

    Use this when you need to test RLS behavior within the same session
    that created fixture data. Switches from shandy_admin to shandy_app.
    """
    await session.execute(text("SET ROLE shandy_app"))


def fake_admin_session(session_obj: Any) -> Any:
    """Build an async context manager that yields the provided session.

    Useful for monkeypatching ``get_admin_session`` in tests so that the
    test's own database session is used instead of creating a new one.
    """

    @asynccontextmanager
    async def _ctx() -> AsyncIterator[Any]:
        yield session_obj

    return _ctx

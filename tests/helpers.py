"""Shared test utilities.

This module contains helper functions used across multiple test files.
These are separated from conftest.py so they can be imported without
mypy resolution issues.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def enable_rls(session: AsyncSession) -> None:
    """Switch session from admin role to app role (enables RLS enforcement).

    Use this when you need to test RLS behavior within the same session
    that created fixture data. Switches from shandy_admin to shandy_app.
    """
    await session.execute(text("SET ROLE shandy_app"))

"""Database errors reach the logs, so they must not carry the values a statement
bound, such as session tokens (plans/token-findings.md)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.session import async_session_local, thread_safe_admin_session

_SECRET = "session-token-3f1c9a7e-not-for-logs"

pytestmark = pytest.mark.usefixtures("test_engine")


@pytest.mark.parametrize(
    "open_session",
    [async_session_local, thread_safe_admin_session],
    ids=["web login", "admin login"],
)
async def test_a_failed_statement_does_not_reveal_what_it_bound(
    open_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> None:
    async with open_session() as session:
        with pytest.raises(DBAPIError) as failure:
            await session.execute(text("SELECT CAST(:value AS text), 1 / 0"), {"value": _SECRET})
    assert "division by zero" in str(failure.value)
    assert _SECRET not in str(failure.value)

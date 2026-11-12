"""A slow page build is not a database outage.

The deadline in ``require_auth`` bounds the session check against a database
that accepts connects and never answers. The page build itself keeps running
past it, and NiceGUI bounds the build with the page's own ``response_timeout``:
a build that outlives that is cancelled and answered with NiceGUI's own 500
page, not the outage page.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from nicegui import app, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Session, User
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenPage

#: What the auth deadline is of a default page, whose ``response_timeout`` is
#: 3.0 s: that minus the 0.5 s kept for sending the outage page.
_CHECK_DEADLINE_SECONDS = 2.5
#: Longer than the check deadline, short enough that NiceGUI's own
#: ``response_timeout`` still lets the build through.
_SLOW_BUILD_SECONDS = 2.7

_BUILT = "slow page built"
_OUTAGE = "OpenScientist cannot reach its database right now."


@pytest_asyncio.fixture
async def login(test_engine: AsyncEngine) -> AsyncIterator[str]:
    """A live session of an approved user, committed the way a sign-in does."""
    email = f"slow-{uuid4().hex}@build.test"
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=email, name="Slow Builder", is_approved=True)
        db.add(user)
        await db.flush()
        session, session_token = Session.issue(
            user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(session)
        await db.commit()
    yield session_token
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.email == email))
        await db.commit()


def _label_texts(client: Client) -> list[str]:
    return [label.text for label in client.layout.descendants() if isinstance(label, ui.label)]


async def test_a_slow_build_after_the_session_check_is_not_an_outage(
    database: Forwarder,
    login: str,
    signed_in_page: OpenPage,
) -> None:
    """The session check answers in time and only the build is slow, so the
    visitor gets their page, not the outage page."""

    async def build() -> None:
        await asyncio.sleep(_SLOW_BUILD_SECONDS)
        ui.label(_BUILT)

    started = time.monotonic()
    client = await signed_in_page(login, build)
    took = time.monotonic() - started

    assert took >= _CHECK_DEADLINE_SECONDS, "the build did not outlive the check deadline"
    assert _BUILT in _label_texts(client), "the slow build was replaced instead of served"
    assert _OUTAGE not in _label_texts(client)
    assert app.storage.user["authenticated"] is True

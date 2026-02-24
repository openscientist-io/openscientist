"""Pytest fixtures for webapp tests."""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Session, User


@pytest.fixture(autouse=True)
def _nicegui_storage_dir(tmp_path):
    """Provide a temporary directory for NiceGUI storage during tests.

    NiceGUI persists user storage to .nicegui/ on disk. Without this,
    teardown fails with FileNotFoundError when async_backup runs.
    """
    storage_dir = tmp_path / ".nicegui"
    storage_dir.mkdir()
    with patch.dict(os.environ, {"NICEGUI_STORAGE_PATH": str(storage_dir)}):
        yield


@pytest_asyncio.fixture
async def webapp_user(db_session: AsyncSession) -> User:
    """Create a test user for webapp tests."""
    user = User(
        email="webapp-test@example.com",
        name="Webapp Test User",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def webapp_session(db_session: AsyncSession, webapp_user: User) -> Session:
    """Create a valid session for the webapp test user."""
    session = Session(
        user_id=webapp_user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        ip_address="127.0.0.1",
        user_agent="pytest-test-agent",
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session

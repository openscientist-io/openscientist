"""The /skill/<category>/<slug> page built in a real signed-in NiceGUI client,
read at the moment NiceGUI sends it (see ``served_page``)."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth.middleware import _store_authenticated_user, validate_session
from openscientist.database.models import Session, Skill, User
from openscientist.webapp_components.pages.skill_detail import skill_detail_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage


@pytest_asyncio.fixture
async def skill_reader(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An approved user's live session and one enabled skill, committed."""
    category = f"flashtest-{uuid4().hex[:8]}"
    slug = f"survival-{uuid4().hex[:8]}"
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"reader-{uuid4().hex}@skill.test", name="Reader", is_approved=True)
        skill = Skill(
            name="Survival analysis",
            slug=slug,
            category=category,
            description="Kaplan-Meier and Cox models",
            content="# Survival",
            content_hash=uuid4().hex,
        )
        db.add_all([user, skill])
        await db.flush()
        login, login_token = Session.issue(
            user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(login)
        await db.commit()
    yield {"token": login_token, "category": category, "slug": slug}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Skill).where(Skill.id == skill.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


def _labels(client: Client) -> list[str]:
    return [e.text for e in client.elements.values() if isinstance(e, ui.label)]


@pytest.mark.asyncio
async def test_the_first_html_already_holds_the_skill(
    database: Forwarder, skill_reader: dict[str, str], served_page: ServedPage
) -> None:
    async def build() -> None:
        await skill_detail_page(skill_reader["category"], skill_reader["slug"])

    _, sent = await served_page(skill_reader["token"], build, _labels)
    assert "Kaplan-Meier and Cox models" in sent
    assert not [text for text in sent if text.startswith("Loading")]


@pytest.mark.asyncio
async def test_the_first_html_already_says_an_unknown_skill_is_not_found(
    database: Forwarder, skill_reader: dict[str, str], served_page: ServedPage
) -> None:
    async def build() -> None:
        await skill_detail_page(skill_reader["category"], "no-such-skill")

    _, sent = await served_page(skill_reader["token"], build, _labels)
    assert "Skill not found" in sent


@pytest.mark.asyncio
async def test_the_first_html_already_says_the_skill_failed_to_load(
    database: Forwarder, skill_reader: dict[str, str], served_page: ServedPage
) -> None:
    """The database goes away once the visitor is signed in, while the skill loads."""

    async def build_without_database() -> None:
        user_info = await validate_session(skill_reader["token"])
        assert user_info is not None
        _store_authenticated_user(user_info)
        database.down()
        await inspect.unwrap(skill_detail_page)(skill_reader["category"], skill_reader["slug"])

    _, sent = await served_page(skill_reader["token"], build_without_database, _labels)
    assert "Failed to load skill" in sent

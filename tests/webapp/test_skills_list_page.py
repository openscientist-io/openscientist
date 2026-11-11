"""The /skills page built in a real signed-in NiceGUI client, read at the
moment NiceGUI sends it (see ``served_page``)."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ElementFilter, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth.middleware import _store_authenticated_user, validate_session
from openscientist.database.models import Session, Skill, User
from openscientist.webapp_components.pages.skills_list import skills_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage


@pytest_asyncio.fixture
async def skills_reader(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An approved user's live session and one enabled skill in a category of its own."""
    category = f"flashtest-{uuid4().hex[:8]}"
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"reader-{uuid4().hex}@skills.test", name="Reader", is_approved=True)
        skill = Skill(
            name="Survival analysis",
            slug=f"survival-{uuid4().hex[:8]}",
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
    yield {"token": login_token, "skill_id": str(skill.id), "category": category}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Skill).where(Skill.id == skill.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


def _sent(client: Client) -> dict[str, Any]:
    table = next(e for e in client.elements.values() if isinstance(e, ui.table))
    select = next(e for e in client.elements.values() if isinstance(e, ui.select))
    return {
        "skill_ids": {row["id"] for row in table.rows},
        "categories": list(select.options),
    }


def _marked(client: Client, marker: str) -> ui.element:
    with client:
        return next(iter(ElementFilter(marker=marker)))


@pytest.mark.asyncio
async def test_the_first_html_already_holds_the_skills_and_categories(
    database: Forwarder, skills_reader: dict[str, str], served_page: ServedPage
) -> None:
    client, sent = await served_page(skills_reader["token"], skills_page, _sent)
    assert skills_reader["skill_id"] in sent["skill_ids"]
    assert skills_reader["category"] in sent["categories"]
    assert not _marked(client, "skills-load-error").visible


@pytest.mark.asyncio
async def test_a_failed_load_says_so_instead_of_an_empty_table(
    database: Forwarder, skills_reader: dict[str, str], served_page: ServedPage
) -> None:
    """The database goes away once the visitor is signed in, while the skills load."""

    async def build_without_database() -> None:
        user_info = await validate_session(skills_reader["token"])
        assert user_info is not None
        _store_authenticated_user(user_info)
        database.down()
        await inspect.unwrap(skills_page)()

    client, sent = await served_page(skills_reader["token"], build_without_database, _sent)
    assert sent["skill_ids"] == set()
    error = _marked(client, "skills-load-error")
    assert error.visible
    table = next(e for e in client.elements.values() if isinstance(e, ui.table))
    assert not table.visible

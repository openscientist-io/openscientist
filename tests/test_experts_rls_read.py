"""Tests for the experts_read RLS policy and _expert_rows formatting."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Expert


@pytest.mark.asyncio
async def test_app_role_can_select_experts(db_session_rls: AsyncSession) -> None:
    """App role (subject to RLS) can read seeded experts via experts_read policy."""
    result = await db_session_rls.execute(select(Expert).where(Expert.is_enabled.is_(True)))
    rows = list(result.scalars().all())
    assert len(rows) >= 8


@pytest.mark.asyncio
async def test_app_role_cannot_insert_experts(db_session_rls: AsyncSession) -> None:
    """App role hits RLS violation on INSERT (writes are admin-only)."""
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        await db_session_rls.execute(
            text(
                "INSERT INTO experts (slug, name, description, prompt, category, source) "
                "VALUES ('rls-test', 'x', 'x', 'x', 'code', 'openscientist')"
            )
        )
    await db_session_rls.rollback()


@pytest.mark.asyncio
async def test_load_experts_helper_returns_seeded_rows(db_session: AsyncSession) -> None:
    """_expert_rows serializes seeded experts with all expected fields."""
    from openscientist.webapp_components.pages.skills_list import _expert_rows

    result = await db_session.execute(
        select(Expert).where(Expert.is_enabled.is_(True)).order_by(Expert.slug)
    )
    rows = _expert_rows(list(result.scalars().all()))

    assert len(rows) >= 8
    slugs = {r["slug"] for r in rows}
    assert "research-lead" in slugs
    assert "data-scientist" in slugs
    for row in rows:
        assert row["name"]
        assert row["description"]
        assert row["category"]
        assert row["category_color"]
        assert row["source"]


@pytest.mark.asyncio
async def test_expert_description_is_truncated(db_session: AsyncSession) -> None:
    """Descriptions longer than 120 chars are truncated with '...'."""
    from openscientist.webapp_components.pages.skills_list import _expert_rows

    result = await db_session.execute(
        select(Expert).where(Expert.is_enabled.is_(True)).order_by(Expert.slug)
    )
    rows = _expert_rows(list(result.scalars().all()))

    for row in rows:
        assert len(row["description"]) <= 123  # 120 + "..."

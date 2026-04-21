"""Tests for the Expert SQLAlchemy model."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Expert


def _minimal_expert(**overrides: object) -> Expert:
    """Build an Expert with only the required fields populated."""
    defaults: dict[str, object] = {
        "slug": "test-expert",
        "name": "Test Expert",
        "description": "A test expert that delegates are routed to.",
        "prompt": "You are a test expert. Do the thing.",
        "category": "research",
        "source": "openscientist",
    }
    defaults.update(overrides)
    return Expert(**defaults)


@pytest.mark.asyncio
async def test_expert_minimal_row_roundtrip(db_session: AsyncSession) -> None:
    """Inserting with only required fields populates defaults and nullables."""
    expert = _minimal_expert()
    db_session.add(expert)
    await db_session.commit()
    await db_session.refresh(expert)

    assert expert.id is not None
    assert isinstance(expert.created_at, datetime)
    assert isinstance(expert.updated_at, datetime)
    assert expert.is_enabled is True
    # Nullable fields default to None
    assert expert.tools is None
    assert expert.model is None
    assert expert.source_url is None


@pytest.mark.asyncio
async def test_expert_slug_uniqueness(db_session: AsyncSession) -> None:
    """Two rows with the same slug must raise IntegrityError."""
    db_session.add(_minimal_expert(slug="dup"))
    await db_session.commit()

    db_session.add(_minimal_expert(slug="dup", name="Other"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    # Release the deactivated transaction so fixture teardown can rollback cleanly.
    await db_session.rollback()


@pytest.mark.asyncio
async def test_expert_tools_stores_string_list(db_session: AsyncSession) -> None:
    """tools column survives roundtrip as list[str]."""
    tools = [
        "mcp__openscientist-tools__search_pubmed",
        "mcp__openscientist-tools__execute_code",
    ]
    db_session.add(_minimal_expert(slug="has-tools", tools=tools))
    await db_session.commit()

    result = await db_session.execute(select(Expert).where(Expert.slug == "has-tools"))
    loaded = result.scalar_one()
    assert loaded.tools == tools


@pytest.mark.asyncio
async def test_expert_nullable_fields_accept_none(db_session: AsyncSession) -> None:
    """tools, model, source_url may all be None."""
    db_session.add(
        _minimal_expert(
            slug="all-null",
            tools=None,
            model=None,
            source_url=None,
        )
    )
    await db_session.commit()

    result = await db_session.execute(select(Expert).where(Expert.slug == "all-null"))
    loaded = result.scalar_one()
    assert loaded.tools is None
    assert loaded.model is None
    assert loaded.source_url is None


@pytest.mark.asyncio
async def test_expert_missing_slug_raises(db_session: AsyncSession) -> None:
    """Omitting the non-nullable slug column must raise IntegrityError."""
    expert = Expert(
        name="no slug",
        description="x",
        prompt="x",
        category="research",
        source="openscientist",
    )
    db_session.add(expert)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_expert_missing_prompt_raises(db_session: AsyncSession) -> None:
    """Omitting the non-nullable prompt column must raise IntegrityError."""
    expert = Expert(
        slug="no-prompt",
        name="no prompt",
        description="x",
        category="research",
        source="openscientist",
    )
    db_session.add(expert)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_expert_is_enabled_default_true(db_session: AsyncSession) -> None:
    """When is_enabled is not passed, default must be True."""
    expert = _minimal_expert(slug="defaulted")
    db_session.add(expert)
    await db_session.commit()
    await db_session.refresh(expert)
    assert expert.is_enabled is True


@pytest.mark.asyncio
async def test_expert_disabled_row(db_session: AsyncSession) -> None:
    """Explicitly disabled row round-trips with is_enabled=False."""
    expert = _minimal_expert(slug="disabled", is_enabled=False)
    db_session.add(expert)
    await db_session.commit()
    await db_session.refresh(expert)
    assert expert.is_enabled is False

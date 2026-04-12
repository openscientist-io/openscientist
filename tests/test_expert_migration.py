"""Tests that the Alembic migration for the expert table applied correctly."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_expert_table_exists(db_session: AsyncSession) -> None:
    """The `experts` table must be created by the migration."""
    result = await db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'experts'"
        )
    )
    assert result.scalar_one_or_none() == "experts"


@pytest.mark.asyncio
async def test_expert_columns_match_spec(db_session: AsyncSession) -> None:
    """All expected columns exist with the expected nullability."""
    result = await db_session.execute(
        text(
            "SELECT column_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'experts' "
            "ORDER BY column_name"
        )
    )
    columns = {row.column_name: row.is_nullable for row in result}

    expected = {
        "id": "NO",
        "slug": "NO",
        "name": "NO",
        "description": "NO",
        "prompt": "NO",
        "category": "NO",
        "source": "NO",
        "tools": "YES",
        "model": "YES",
        "source_url": "YES",
        "is_enabled": "NO",
        "created_at": "NO",
        "updated_at": "NO",
    }
    assert columns == expected


@pytest.mark.asyncio
async def test_expert_slug_unique_index(db_session: AsyncSession) -> None:
    """The slug column must have a unique index."""
    result = await db_session.execute(
        text(
            "SELECT indexname, indexdef "
            "FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'experts'"
        )
    )
    indexes = {row.indexname: row.indexdef for row in result}
    slug_unique_indexes = [
        name for name, ddl in indexes.items() if "slug" in ddl.lower() and "unique" in ddl.lower()
    ]
    assert slug_unique_indexes, f"No unique index on slug found. Indexes: {indexes}"


@pytest.mark.asyncio
async def test_expert_model_check_constraint_rejects_unknown(
    db_session: AsyncSession,
) -> None:
    """CHECK constraint rejects invalid model values."""
    from sqlalchemy.exc import IntegrityError

    stmt = text(
        "INSERT INTO experts "
        "(slug, name, description, prompt, category, source, model) "
        "VALUES ('bad-model', 'bad', 'bad', 'bad', 'code', 'openscientist', 'gpt-4')"
    )
    with pytest.raises(IntegrityError):
        await db_session.execute(stmt)
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_expert_model_check_constraint_accepts_known_values(
    db_session: AsyncSession,
) -> None:
    """The CHECK constraint must accept all four valid model literals and NULL."""
    for model in ("sonnet", "opus", "haiku", "inherit"):
        stmt = text(
            "INSERT INTO experts "
            "(slug, name, description, prompt, category, source, model) "
            f"VALUES ('ok-{model}', 'ok', 'ok', 'ok', 'code', 'openscientist', '{model}')"
        )
        await db_session.execute(stmt)
    stmt = text(
        "INSERT INTO experts "
        "(slug, name, description, prompt, category, source, model) "
        "VALUES ('ok-null', 'ok', 'ok', 'ok', 'code', 'openscientist', NULL)"
    )
    await db_session.execute(stmt)
    await db_session.commit()


@pytest.mark.asyncio
async def test_expert_tools_check_constraint_rejects_non_array(
    db_session: AsyncSession,
) -> None:
    """CHECK constraint rejects non-array tools values."""
    from sqlalchemy.exc import IntegrityError

    stmt = text(
        "INSERT INTO experts "
        "(slug, name, description, prompt, category, source, tools) "
        "VALUES ('bad-tools', 'bad', 'bad', 'bad', 'code', 'openscientist', "
        "'\"not an array\"'::jsonb)"
    )
    with pytest.raises(IntegrityError):
        await db_session.execute(stmt)
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_expert_tools_column_is_jsonb(db_session: AsyncSession) -> None:
    """The tools column must be JSONB to store list[str]."""
    result = await db_session.execute(
        text(
            "SELECT data_type, udt_name "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND table_name = 'experts' AND column_name = 'tools'"
        )
    )
    row = result.one()
    assert row.udt_name == "jsonb"

"""Assert the session-scoped migrated test DB reached current Alembic head.

Reuses ``tests/conftest.py`` fixtures that already apply ``alembic upgrade head``
once per session. This module does not drop schemas or re-run migrations.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Current Alembic head (see database/migrations/README.md revision chain).
# Chain: init_full_schema -> add_review_tokens -> rename_title_to_rq
#        -> add_pubmed_mirror -> add_token_buckets -> add_version_info
EXPECTED_HEAD_REVISION = "add_version_info"

# Representative tables from across the chain:
# - init_full_schema: users, jobs, skills
# - add_review_tokens: review_tokens
# - add_pubmed_mirror: pubmed_articles
# (add_token_buckets and add_version_info add columns, not tables)
KEY_TABLES = ("users", "jobs", "skills", "review_tokens", "pubmed_articles")


@pytest.mark.asyncio
async def test_alembic_head_and_key_tables(db_session: AsyncSession) -> None:
    """Migrated test DB is stamped at head and exposes key chain tables."""
    version_row = (await db_session.execute(text("SELECT version_num FROM alembic_version"))).one()
    assert version_row[0] == EXPECTED_HEAD_REVISION

    for table_name in KEY_TABLES:
        exists = (
            await db_session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = :table_name
                    )
                    """
                ),
                {"table_name": table_name},
            )
        ).scalar_one()
        assert exists is True, f"expected table public.{table_name} after upgrade head"

    # rename_title_to_rq: jobs.title -> jobs.research_question
    has_research_question = (
        await db_session.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'jobs'
                      AND column_name = 'research_question'
                )
                """
            )
        )
    ).scalar_one()
    assert has_research_question is True

"""The loader must ingest as the role that owns the pubmed objects.

Regression test for a clean-install failure: docker-compose points
ADMIN_DATABASE_URL at ``openscientist_admin``, which has DML grants and
BYPASSRLS but does not own ``ix_pubmed_articles_search_vector`` (migrations run
as the DATABASE_URL role, which owns what they create). The ingest drops and
rebuilds that index, and only its owner may — so ``loader_dsn`` must pick the
migration role's DSN, not the admin DSN.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.pubmed_mirror.__main__ import loader_dsn
from openscientist.pubmed_mirror.ingest import ingest_files
from openscientist.settings import DatabaseSettings

_ADMIN_PASSWORD = "pubmed-ingest-test-password"

_SAMPLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>901</PMID>
      <Article>
        <Journal>
          <Title>Journal of Fish Biology</Title>
          <JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Zebrafish thermal tolerance</ArticleTitle>
        <Abstract><AbstractText>Warm water acclimation.</AbstractText></Abstract>
        <AuthorList><Author><LastName>Doe</LastName></Author></AuthorList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>902</PMID>
      <Article>
        <Journal>
          <JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Soil microbiome depth gradients</ArticleTitle>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


@pytest.fixture
def baseline_file(tmp_path: Path) -> Path:
    """A tiny two-article baseline file (name encodes baseline year 2026)."""
    path = tmp_path / "pubmed26n0001.xml"
    path.write_text(_SAMPLE_XML)
    return path


@pytest_asyncio.fixture
async def admin_login_url(
    test_engine: AsyncEngine, test_database_url: str
) -> AsyncGenerator[str, None]:
    """A LOGIN DSN for openscientist_admin, as docker-compose configures it.

    conftest creates the role NOLOGIN (tests normally SET ROLE into it); enable
    LOGIN so the ingest can dial it directly, and revert afterwards.
    """
    async with test_engine.begin() as conn:
        await conn.execute(
            text(f"ALTER ROLE openscientist_admin WITH LOGIN PASSWORD '{_ADMIN_PASSWORD}'")
        )
    url = make_url(test_database_url).set(username="openscientist_admin", password=_ADMIN_PASSWORD)
    yield url.render_as_string(hide_password=False)
    async with test_engine.begin() as conn:
        await conn.execute(text("ALTER ROLE openscientist_admin WITH NOLOGIN PASSWORD NULL"))


@pytest_asyncio.fixture
async def clean_pubmed_tables(test_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Leave the session-shared corpus tables empty for later tests."""
    yield
    async with test_engine.begin() as conn:
        await conn.execute(text("TRUNCATE pubmed_articles, pubmed_corpus_meta"))


@pytest.mark.asyncio
async def test_ingest_over_loader_dsn_rebuilds_search_index(
    test_database_url: str,
    admin_login_url: str,
    baseline_file: Path,
    test_engine: AsyncEngine,
    clean_pubmed_tables: None,
) -> None:
    """With ADMIN_DATABASE_URL set to the non-owner admin role (the
    docker-compose clean-install shape), the DSN main() hands to ingest_files
    must still be able to drop and rebuild the search index."""
    settings = DatabaseSettings(
        DATABASE_URL=test_database_url,
        ADMIN_DATABASE_URL=admin_login_url,
    )

    total = await ingest_files([baseline_file], dsn=loader_dsn(settings))

    assert total == 2
    async with test_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM pubmed_articles")) == 2
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_indexes"
                    " WHERE tablename = 'pubmed_articles'"
                    " AND indexname = 'ix_pubmed_articles_search_vector'"
                )
            )
            == 1
        )
        meta = (
            await conn.execute(
                text(
                    "SELECT baseline_year, article_count FROM pubmed_corpus_meta"
                    " ORDER BY id DESC LIMIT 1"
                )
            )
        ).one()
        assert tuple(meta) == (2026, 2)


@pytest.mark.asyncio
async def test_ingest_over_admin_dsn_reproduces_clean_install_failure(
    admin_login_url: str,
    baseline_file: Path,
    clean_pubmed_tables: None,
) -> None:
    """The pre-fix DSN choice: the admin role cannot DROP the owner's index, so
    ingesting over ADMIN_DATABASE_URL fails. Proves the test above is not
    passing vacuously (the admin role is genuinely not the owner here)."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await ingest_files([baseline_file], dsn=admin_login_url)

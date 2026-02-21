"""
Tests for skill sync scheduler caching behavior.

Tests that the scheduler correctly uses DB-based rate limiting
and passes force through to the ingester.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import SkillSource
from shandy.skill_scheduler import SkillSyncScheduler


class TestSkillSyncSchedulerCaching:
    """Tests for scheduler rate-limiting and force plumbing."""

    @pytest.mark.asyncio
    async def test_skips_recently_synced_source_from_db(
        self,
        db_session: AsyncSession,
    ):
        """Scheduler skips a source whose DB last_synced_at is recent, even with empty in-memory cache."""
        source = SkillSource(
            name="Recent Source",
            source_type="github",
            url="https://github.com/owner/repo",
            branch="main",
            skills_path="skills",
            is_enabled=True,
            last_synced_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        scheduler = SkillSyncScheduler(github_token="fake-token")
        # Ensure in-memory cache is empty
        assert scheduler._last_sync == {}

        result = await scheduler._sync_source_if_needed(db_session, source)

        # Should skip — no sync performed
        assert result is None
        # Should have backfilled the in-memory cache
        assert str(source.id) in scheduler._last_sync

    @pytest.mark.asyncio
    async def test_sync_source_by_id_passes_force(
        self,
        db_session: AsyncSession,
    ):
        """sync_source_by_id passes force=True, bypassing rate limits and SHA check."""
        source = SkillSource(
            name="Force Source",
            source_type="github",
            url="https://github.com/owner/repo",
            branch="main",
            skills_path="skills",
            is_enabled=True,
            last_synced_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            last_commit_sha="same_sha",
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)
        source_id = str(source.id)

        scheduler = SkillSyncScheduler(github_token="fake-token")

        mock_stats = {"created": 0, "updated": 0, "unchanged": 0, "errors": 0}

        with (
            patch(
                "shandy.skill_scheduler.sync_skill_source",
                new_callable=AsyncMock,
                return_value=mock_stats,
            ) as mock_sync,
            patch(
                "shandy.skill_scheduler.get_admin_session",
            ) as mock_get_session,
        ):
            # Make get_admin_session return a context manager that yields our db_session
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = db_session
            mock_ctx.__aexit__.return_value = None
            mock_get_session.return_value = mock_ctx

            result = await scheduler.sync_source_by_id(source_id)

            assert result is not None
            assert result.success is True
            # Verify force=True was passed through to sync_skill_source
            mock_sync.assert_called_once()
            call_kwargs = mock_sync.call_args.kwargs
            assert call_kwargs["force"] is True

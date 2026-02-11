"""
Skill sync scheduler for periodic background synchronization.

Runs as a background task that periodically syncs skills from configured sources.
Uses caching based on commit SHA to avoid redundant syncs.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database.models import SkillSource
from .database.rls import bypass_rls
from .database.session import get_session
from .settings import get_settings
from .skill_ingestion import sync_skill_source

logger = logging.getLogger(__name__)

# Default sync interval (1 hour)
DEFAULT_SYNC_INTERVAL_SECONDS = 3600

# Minimum time between syncs for the same source (to avoid hammering)
MIN_SYNC_INTERVAL_SECONDS = 300  # 5 minutes


@dataclass
class SyncResult:
    """Result of a sync operation."""

    source_id: str
    source_name: str
    success: bool
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    error_message: Optional[str] = None


class SkillSyncScheduler:
    """
    Background scheduler for periodic skill synchronization.

    Features:
    - Runs sync on startup
    - Periodic sync at configurable intervals
    - Caching based on commit SHA (skips if unchanged)
    - Rate limiting to avoid hammering sources
    - Graceful shutdown
    """

    def __init__(
        self,
        sync_interval: int = DEFAULT_SYNC_INTERVAL_SECONDS,
        github_token: Optional[str] = None,
    ):
        """
        Initialize the scheduler.

        Args:
            sync_interval: Seconds between sync runs
            github_token: Optional GitHub token (uses settings.provider.github_token if not provided)
        """
        self.sync_interval = sync_interval
        self.github_token = github_token or get_settings().provider.github_token
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_sync: dict[str, datetime] = {}  # source_id -> last sync time

    async def start(self) -> None:
        """Start the background sync task."""
        if self._running:
            logger.warning("Skill sync scheduler already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Skill sync scheduler started (interval: %d seconds)",
            self.sync_interval,
        )

    async def stop(self) -> None:
        """Stop the background sync task gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Skill sync scheduler stopped")

    async def _run_loop(self) -> None:
        """Main loop that runs sync periodically."""
        # Run initial sync on startup
        try:
            await self.sync_all_sources()
        except Exception as e:
            logger.error("Initial skill sync failed: %s", e)

        # Then run periodically
        while self._running:
            try:
                await asyncio.sleep(self.sync_interval)
                if self._running:
                    await self.sync_all_sources()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Skill sync failed: %s", e, exc_info=True)

    async def sync_all_sources(self) -> list[SyncResult]:
        """
        Sync all enabled skill sources.

        Returns:
            List of SyncResult for each source
        """
        results: list[SyncResult] = []

        try:
            async with get_session() as session:
                sources = await self._get_enabled_sources(session)
                logger.info("Found %d enabled skill sources", len(sources))

                for source in sources:
                    result = await self._sync_source_if_needed(session, source)
                    if result:
                        results.append(result)

        except Exception as e:
            logger.error("Failed to sync skill sources: %s", e, exc_info=True)

        return results

    async def _get_enabled_sources(
        self,
        session: AsyncSession,
    ) -> list[SkillSource]:
        """Get all enabled skill sources."""
        async with bypass_rls(session):
            stmt = select(SkillSource).where(SkillSource.is_enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _sync_source_if_needed(
        self,
        session: AsyncSession,
        source: SkillSource,
    ) -> Optional[SyncResult]:
        """
        Sync a source if it needs syncing.

        Checks:
        1. Rate limiting (MIN_SYNC_INTERVAL_SECONDS)
        2. Commit SHA cache (skip if unchanged for GitHub sources)

        Args:
            session: Database session
            source: SkillSource to sync

        Returns:
            SyncResult if synced, None if skipped
        """
        source_id_str = str(source.id)

        # Rate limiting check
        last_sync = self._last_sync.get(source_id_str)
        if last_sync:
            elapsed = (datetime.now(timezone.utc) - last_sync).total_seconds()
            if elapsed < MIN_SYNC_INTERVAL_SECONDS:
                logger.debug(
                    "Skipping %s (synced %d seconds ago)",
                    source.name,
                    int(elapsed),
                )
                return None

        try:
            logger.info("Syncing skill source: %s", source.name)

            stats = await sync_skill_source(
                session=session,
                source=source,
                github_token=self.github_token,
            )

            # Update last sync time
            self._last_sync[source_id_str] = datetime.now(timezone.utc)

            result = SyncResult(
                source_id=source_id_str,
                source_name=source.name,
                success=True,
                created=stats.get("created", 0),
                updated=stats.get("updated", 0),
                unchanged=stats.get("unchanged", 0),
                errors=stats.get("errors", 0),
            )

            logger.info(
                "Synced %s: created=%d, updated=%d, unchanged=%d, errors=%d",
                source.name,
                result.created,
                result.updated,
                result.unchanged,
                result.errors,
            )

            return result

        except Exception as e:
            logger.error("Failed to sync %s: %s", source.name, e)

            # Update source with error
            async with bypass_rls(session):
                source.sync_error = str(e)
                await session.commit()

            return SyncResult(
                source_id=source_id_str,
                source_name=source.name,
                success=False,
                error_message=str(e),
            )

    async def sync_source_by_id(self, source_id: str) -> Optional[SyncResult]:
        """
        Manually trigger sync for a specific source.

        Args:
            source_id: UUID of the source to sync

        Returns:
            SyncResult or None if source not found
        """
        from uuid import UUID

        try:
            source_uuid = UUID(source_id)
        except ValueError:
            logger.warning("Invalid source ID: %s", source_id)
            return None

        async with get_session() as session:
            async with bypass_rls(session):
                stmt = select(SkillSource).where(SkillSource.id == source_uuid)
                result = await session.execute(stmt)
                source = result.scalar_one_or_none()

                if not source:
                    logger.warning("Source not found: %s", source_id)
                    return None

                # Force sync (bypass rate limiting)
                self._last_sync.pop(source_id, None)
                return await self._sync_source_if_needed(session, source)


# Global scheduler instance
_scheduler: Optional[SkillSyncScheduler] = None


def get_scheduler() -> SkillSyncScheduler:
    """Get the global scheduler instance, creating if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SkillSyncScheduler()
    return _scheduler


async def start_skill_scheduler() -> None:
    """Start the global skill sync scheduler."""
    scheduler = get_scheduler()
    await scheduler.start()


async def stop_skill_scheduler() -> None:
    """Stop the global skill sync scheduler."""
    global _scheduler
    if _scheduler:
        await _scheduler.stop()
        _scheduler = None

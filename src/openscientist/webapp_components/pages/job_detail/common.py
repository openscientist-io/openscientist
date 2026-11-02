"""Shared context and knowledge-state loaders for the job detail page."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from openscientist.database.session import thread_safe_user_session
from openscientist.job.types import completed_iterations
from openscientist.knowledge_state import KnowledgeState

logger = logging.getLogger(__name__)


def _derive_progress_from_ks(
    ks_data: dict[str, Any] | None,
    status: str,
    default_iterations: int,
) -> tuple[int, int]:
    """Derive iteration/findings counts from already-loaded KS data."""
    if ks_data is None:
        return default_iterations, 0
    findings_count = len(ks_data.get("findings", []))
    ks_iteration = int(ks_data.get("iteration", 1))
    return completed_iterations(status, ks_iteration), findings_count


async def _load_knowledge_state(
    job_id: str, user_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Load knowledge state from database, returning (data, error_message)."""
    try:
        async with thread_safe_user_session(UUID(user_id)) as session:
            ks = await KnowledgeState.load_from_database(job_id, session)
        return ks.to_dict(), None
    except Exception as e:
        logger.warning("Failed to load knowledge state from database for %s: %s", job_id, e)
        return None, "Knowledge state is unavailable. Please refresh the page."


@dataclass
class _JobDetailContext:
    job_id: str
    user_id: str
    job_manager: Any
    job_info: Any
    db_job: Any
    is_owner: bool
    can_edit: bool
    owner_name: str | None
    job_dir: Path
    ks_data: dict[str, Any] | None
    ks_load_error: str | None
    state: dict[str, Any]
    active_timers: list[Any]
    share_dialog: Any
    delete_dialog: Any
    notifications_dialog: Any

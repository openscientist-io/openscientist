"""The web's budget enforcement: a verdict against the cost ledger and the
provider's billed total, read at each moment a new unit of work is allowed."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.admin_settings import current_admin_settings
from openscientist.database.models import CostRecord
from openscientist.database.session import get_admin_session
from openscientist.providers import get_provider
from openscientist.providers.base import CostInfo
from openscientist.providers.pricing import has_price

logger = logging.getLogger(__name__)

#: The providers a job may run on without the deployment's limits being able to
#: see its spend, exempt from the unpriced-model refusal.
SELF_HOSTED_PROVIDER_IDS = frozenset({"ollama", "vllm", "llamacpp"})

#: A provider's billed total stays fresh for this long; the ledger is read live.
BILLING_TTL_SECONDS = 600

_limit = Literal["job_cap", "project_total", "project_24h", "unpriced_model"]

_billed: CostInfo | None = None
_billed_at: float = 0.0


@dataclass(frozen=True)
class BudgetVerdict:
    """The outcome of one budget check, the limit it refused on and its message."""

    ok: bool
    limit: _limit | None
    message: str | None


@dataclass(frozen=True)
class ProjectSpend:
    """The project spend the enforcement compares against the project limits.

    Each figure is the higher of the ledger's and the provider's billed value,
    and None on the billing side means the provider reports no spend, so only
    the ledger counts.
    """

    ledger_total: float
    ledger_24h: float
    billed: CostInfo | None

    @property
    def total(self) -> float:
        if self.billed is None or self.billed.total_spend_usd is None:
            return self.ledger_total
        return max(self.ledger_total, self.billed.total_spend_usd)

    @property
    def last_24h(self) -> float:
        if self.billed is None or self.billed.recent_spend_usd is None:
            return self.ledger_24h
        return max(self.ledger_24h, self.billed.recent_spend_usd)


async def _provider_billing() -> CostInfo | None:
    """The provider's billed total, at most once every 10 minutes per process.

    A failed fetch counts the ledger only and is retried at the next check, so
    it is never cached.
    """
    global _billed, _billed_at
    now = time.monotonic()
    if _billed is not None and now - _billed_at < BILLING_TTL_SECONDS:
        return _billed
    try:
        info = await asyncio.to_thread(get_provider().get_cost_info)
    except Exception as e:
        logger.warning(
            "Could not fetch the provider's billed total, counting the ledger only: %s", e
        )
        return None
    _billed, _billed_at = info, now
    return info


async def _ledger_totals(job_id: UUID | None = None) -> tuple[float, float, float]:
    """The ledger totals the limits read: the job's own, the deployment-wide
    total, and the part of the deployment written in the last 24 hours."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    async with get_admin_session() as session:
        job_total = (
            await session.scalar(
                select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0)).where(
                    CostRecord.job_id == job_id
                )
            )
            if job_id is not None
            else 0.0
        )
        total = await session.scalar(select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0)))
        recent = await session.scalar(
            select(func.coalesce(func.sum(CostRecord.cost_usd), 0.0)).where(
                CostRecord.created_at > cutoff
            )
        )
    return (
        0.0 if job_total is None else float(job_total),
        0.0 if total is None else float(total),
        0.0 if recent is None else float(recent),
    )


def format_spend(spend_usd: float) -> str:
    """The spend figure the badges show: $0.00 for nothing, <$0.01 below one
    cent, $1.23 otherwise."""
    if spend_usd <= 0:
        return "$0.00"
    if spend_usd < 0.01:
        return "<$0.01"
    return f"${spend_usd:.2f}"


async def job_spends(session: AsyncSession, job_ids: Sequence[UUID]) -> dict[UUID, float]:
    """Each job's spend from the cost ledger the session's RLS admits, one
    grouped query for all the ids, 0 for a job without cost rows."""
    if not job_ids:
        return {}
    result = await session.execute(
        select(CostRecord.job_id, func.sum(CostRecord.cost_usd))
        .where(CostRecord.job_id.in_(list(job_ids)))
        .group_by(CostRecord.job_id)
    )
    sums = {job_id: 0.0 if total is None else float(total) for job_id, total in result.all()}
    return {job_id: sums.get(job_id, 0.0) for job_id in job_ids}


async def project_spend() -> ProjectSpend:
    """The project spend the project limits compare against."""
    _, total, recent = await _ledger_totals()
    return ProjectSpend(ledger_total=total, ledger_24h=recent, billed=await _provider_billing())


def warning_limit_reached(spend_24h: float, warn: float | None, hard: float | None) -> bool:
    """The billing page's 24-hour warning card: at or above the warning
    threshold and below the hard 24-hour limit."""
    return warn is not None and spend_24h >= warn and (hard is None or spend_24h < hard)


async def check_budget(
    *,
    job_id: UUID | None = None,
    provider_id: str,
    model: str | None = None,
    include_unpriced: bool = False,
) -> BudgetVerdict:
    """Whether a new unit of work is allowed, the limit it refuses on, and a
    message naming it.

    Checks in order: an unpriced cloud model while any limit is set, the
    job's own cap, the project's total spend, then its 24-hour spend.
    """
    admin = await current_admin_settings()

    if include_unpriced:
        any_limit = (
            admin.max_job_cost_usd is not None
            or admin.max_project_spend_total_usd is not None
            or admin.max_project_spend_24h_usd is not None
            or admin.warn_project_spend_24h_usd is not None
        )
        if (
            any_limit
            and model is not None
            and provider_id.lower() not in SELF_HOSTED_PROVIDER_IDS
            and not has_price(model)
        ):
            return BudgetVerdict(
                False,
                "unpriced_model",
                f"Model {model} has no known price, so the budget cannot be enforced.",
            )

    job_total, total, recent = await _ledger_totals(job_id)

    if (
        job_id is not None
        and admin.max_job_cost_usd is not None
        and job_total >= admin.max_job_cost_usd
    ):
        return BudgetVerdict(
            False,
            "job_cap",
            f"Job cost limit of ${admin.max_job_cost_usd:.2f} reached: "
            f"the job has spent ${job_total:.2f}.",
        )

    if admin.max_project_spend_total_usd is None and admin.max_project_spend_24h_usd is None:
        return BudgetVerdict(True, None, None)

    spend = ProjectSpend(ledger_total=total, ledger_24h=recent, billed=await _provider_billing())
    if (
        admin.max_project_spend_total_usd is not None
        and spend.total >= admin.max_project_spend_total_usd
    ):
        return BudgetVerdict(
            False,
            "project_total",
            f"Project spend total limit of ${admin.max_project_spend_total_usd:.2f} reached: "
            f"the project has spent ${spend.total:.2f}.",
        )
    if (
        admin.max_project_spend_24h_usd is not None
        and spend.last_24h >= admin.max_project_spend_24h_usd
    ):
        return BudgetVerdict(
            False,
            "project_24h",
            f"Project 24-hour spend limit of ${admin.max_project_spend_24h_usd:.2f} reached: "
            f"the project has spent ${spend.last_24h:.2f} in the last 24 hours.",
        )
    return BudgetVerdict(True, None, None)

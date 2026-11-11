"""Model-call reservations and unsettled cost debt for the web process."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from openscientist import budget
from openscientist.agent.base import TokenUsage
from openscientist.database.session import get_admin_session
from openscientist.llm_metering import _ObservedCall, record_model_call
from openscientist.providers.pricing import estimate_cost_usd

if TYPE_CHECKING:
    from openscientist.job_container.secrets import RunKind

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_outstanding: dict[object, ModelCallReservation] = {}


@asynccontextmanager
async def _budget_guard() -> AsyncIterator[None]:
    async with asyncio.timeout(60):
        while not _lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
    try:
        yield
    finally:
        _lock.release()


def _cost(model: str, buckets: TokenUsage) -> float:
    return estimate_cost_usd(
        model,
        buckets.input_tokens,
        buckets.output_tokens,
        cache_read_tokens=buckets.cache_read_tokens,
        cache_write_tokens=buckets.cache_write_tokens,
        cache_write_1h_tokens=buckets.cache_write_1h_tokens,
        reasoning_tokens=buckets.reasoning_tokens,
    )


@dataclass
class _Debt:
    model: str
    buckets: TokenUsage
    cost: float
    estimated: bool
    created_at: datetime
    record_id: UUID | None = None


@dataclass
class ModelCallReservation:
    """An admitted call whose reserved cost remains charged until settlement."""

    job_id: UUID
    kind: RunKind
    model: str
    provider_id: str
    buckets: TokenUsage
    amount: float
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _id: object = field(default_factory=object)
    _debts: list[_Debt] | None = None
    _ended: tuple[list[_ObservedCall], bool] | None = None
    _closed: bool = False

    async def cancel(self) -> None:
        """Release a reservation for a request that cannot incur a charge."""
        async with _budget_guard():
            if self._closed or self._ended is not None:
                return
            self._closed = True
            _outstanding.pop(self._id, None)

    async def settle(self, calls: list[_ObservedCall], *, complete: bool) -> None:
        """Charge actual usage or a conservative estimate and retain failed writes."""
        if self._closed:
            return
        if self._ended is None:
            self._ended = (calls, complete)
        async with _budget_guard():
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if self._debts is None:
            if self._ended is None:
                return
            calls, complete = self._ended
            costs = [_cost(call.model, call.buckets) for call in calls]
            if complete and calls and all(call.usage_reported for call in calls):
                self._debts = [
                    _Debt(call.model, call.buckets, cost, False, self.created_at)
                    for call, cost in zip(calls, costs, strict=True)
                ]
            else:
                self._debts = [
                    _Debt(
                        self.model,
                        self.buckets,
                        max(self.amount, sum(costs)),
                        True,
                        self.created_at,
                    )
                ]
        for debt in tuple(self._debts):
            if debt.record_id is None:
                try:
                    async with get_admin_session() as session:
                        debt.record_id = (await session.execute(select(func.uuidv7()))).scalar_one()
                except Exception:
                    logger.exception("Could not allocate a cost row for job %s", self.job_id)
                    continue
            if await record_model_call(
                job_id=str(self.job_id),
                kind=self.kind,
                model=debt.model,
                provider_id=self.provider_id,
                buckets=debt.buckets,
                record_id=debt.record_id,
                cost_usd=debt.cost,
                estimated=debt.estimated,
                created_at=debt.created_at,
            ):
                self._debts.remove(debt)
        if not self._debts:
            self._closed = True
            _outstanding.pop(self._id, None)


async def _flush_debt_locked() -> None:
    for reservation in tuple(_outstanding.values()):
        await reservation._flush_locked()


def _pending_totals_locked(
    job_id: UUID | None,
) -> tuple[float, float, float, tuple[UUID, ...]]:
    job_total = total = recent = 0.0
    excluded: list[UUID] = []
    for reservation in _outstanding.values():
        if reservation._debts is None:
            amount = reservation.amount
        else:
            amount = sum(debt.cost for debt in reservation._debts)
            excluded.extend(
                debt.record_id for debt in reservation._debts if debt.record_id is not None
            )
        total += amount
        # Unresolved calls and debt stay in the rolling limit until persisted.
        recent += amount
        if reservation.job_id == job_id:
            job_total += amount
    return job_total, total, recent, tuple(excluded)


async def reserve_model_call(
    *,
    job_id: str,
    kind: RunKind,
    model: str,
    provider_id: str,
    buckets: TokenUsage,
) -> tuple[budget.BudgetVerdict, ModelCallReservation | None]:
    """Atomically admit a call against durable spend, reservations and debt."""
    uid = UUID(job_id)
    amount = _cost(model, buckets)
    async with _budget_guard():
        verdict = await budget._check_budget_locked(
            job_id=uid,
            provider_id=provider_id,
            model=model,
            include_unpriced=True,
            prospective_usd=amount,
        )
        if not verdict.ok:
            return verdict, None
        reservation = ModelCallReservation(uid, kind, model, provider_id, buckets, amount)
        _outstanding[reservation._id] = reservation
        return verdict, reservation

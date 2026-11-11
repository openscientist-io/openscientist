"""Budget reservations survive incomplete calls and failed ledger writes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import budget, llm_metering, llm_reservations
from openscientist.agent.base import TokenUsage
from openscientist.database.models import CostRecord, Job
from openscientist.llm_metering import _ObservedCall
from openscientist.llm_reservations import reserve_model_call
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

MODEL = "reservation-model"
ESTIMATE = TokenUsage(input_tokens=100, output_tokens=100)


@pytest_asyncio.fixture(autouse=True)
async def settings(test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
    await set_admin_settings_row(test_engine)
    monkeypatch.setattr(
        "openscientist.providers.pricing._get_litellm_pricing",
        lambda: {MODEL: {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}},
    )
    monkeypatch.setattr(budget, "_provider_billing", AsyncMock(return_value=None))
    yield
    await reset_admin_settings_row(test_engine)


@pytest_asyncio.fixture
async def jobs(test_engine: AsyncEngine) -> AsyncIterator[tuple[UUID, UUID]]:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        rows = [Job(research_question="Reservation?"), Job(research_question="Other job?")]
        session.add_all(rows)
        await session.commit()
    ids = (rows[0].id, rows[1].id)
    yield ids
    async with test_engine.begin() as conn:
        await conn.execute(delete(CostRecord).where(CostRecord.job_id.in_(ids)))
        await conn.execute(delete(Job).where(Job.id.in_(ids)))


async def reserve(job_id: UUID):
    return await reserve_model_call(
        job_id=str(job_id),
        kind="discovery",
        model=MODEL,
        provider_id="anthropic",
        buckets=ESTIMATE,
    )


async def rows(engine: AsyncEngine, job_id: UUID) -> list[CostRecord]:
    async with AsyncSession(engine) as session:
        return list(
            (await session.scalars(select(CostRecord).where(CostRecord.job_id == job_id))).all()
        )


async def test_aborted_calls_exhaust_job_limit(test_engine: AsyncEngine, jobs):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    verdict, reservation = await reserve(jobs[0])
    assert verdict.ok and reservation is not None
    await reservation.settle([], complete=False)
    verdict = await budget.check_budget(job_id=jobs[0], provider_id="anthropic")
    assert verdict.limit == "job_cap"
    recorded = await rows(test_engine, jobs[0])
    assert len(recorded) == 1
    assert recorded[0].cost_usd == pytest.approx(0.3)
    assert recorded[0].operation_type == "discovery_estimated"


async def test_missing_usage_retains_estimate(test_engine: AsyncEngine, jobs):
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    await reservation.settle(
        [_ObservedCall(MODEL, TokenUsage(), usage_reported=False)], complete=True
    )
    recorded = await rows(test_engine, jobs[0])
    assert recorded[0].cost_usd == pytest.approx(0.3)
    assert recorded[0].operation_type.endswith("_estimated")


async def test_reported_zero_usage_replaces_estimate(test_engine: AsyncEngine, jobs):
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    await reservation.settle([_ObservedCall(MODEL, TokenUsage())], complete=True)
    recorded = await rows(test_engine, jobs[0])
    assert recorded[0].cost_usd == 0
    assert recorded[0].operation_type == "discovery"


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 0, "output_tokens": 0}])
@pytest.mark.parametrize("wire", ["json", "responses"])
def test_decoders_distinguish_missing_and_zero_usage(usage, wire):
    if wire == "json":
        decoder = llm_metering._JsonDecoder(MODEL, llm_metering._anthropic_buckets)
        decoder.feed(json.dumps({"model": MODEL, "usage": usage}).encode())
    else:
        decoder = llm_metering._ResponsesDecoder(MODEL)
        decoder.feed_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {"model": MODEL, "usage": usage},
                }
            ).encode()
        )
    calls, _ = decoder.finalize("completed")
    assert len(calls) == 1
    assert calls[0].usage_reported is bool(usage)


async def test_actual_cost_replaces_reservation_without_double_count(
    test_engine: AsyncEngine, jobs
):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.4)
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    await reservation.settle([_ObservedCall(MODEL, TokenUsage(input_tokens=50))], complete=True)
    await reservation.settle([], complete=False)
    verdict, next_reservation = await reserve(jobs[0])
    assert verdict.ok and next_reservation is not None
    await next_reservation.cancel()
    recorded = await rows(test_engine, jobs[0])
    assert len(recorded) == 1
    assert recorded[0].cost_usd == pytest.approx(0.05)
    assert recorded[0].operation_type == "discovery"


@pytest.mark.parametrize("observed,expected", [(10, 0.3), (400, 0.4)])
async def test_partial_usage_is_conservative(test_engine: AsyncEngine, jobs, observed, expected):
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    await reservation.settle(
        [_ObservedCall(MODEL, TokenUsage(input_tokens=observed))], complete=False
    )
    recorded = await rows(test_engine, jobs[0])
    assert recorded[0].cost_usd == pytest.approx(expected)
    assert recorded[0].operation_type.endswith("_estimated")


async def test_failed_write_counts_and_retries_once(test_engine: AsyncEngine, jobs, monkeypatch):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    writer = llm_reservations.record_model_call
    failed = AsyncMock(return_value=False)
    monkeypatch.setattr(llm_reservations, "record_model_call", failed)
    await reservation.settle([], complete=False)
    assert (await budget.check_budget(job_id=jobs[0], provider_id="anthropic")).limit == "job_cap"
    assert await rows(test_engine, jobs[0]) == []
    monkeypatch.setattr(llm_reservations, "record_model_call", writer)
    for _ in range(2):
        assert (
            await budget.check_budget(job_id=jobs[0], provider_id="anthropic")
        ).limit == "job_cap"
    recorded = await rows(test_engine, jobs[0])
    assert len(recorded) == 1
    assert recorded[0].cost_usd == pytest.approx(0.3)


async def test_ambiguous_commit_never_counts_ledger_and_debt_twice(
    test_engine: AsyncEngine, jobs, monkeypatch
):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.5)
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    writer = llm_reservations.record_model_call

    async def ambiguous(**kwargs):
        assert await writer(**kwargs)
        return False

    monkeypatch.setattr(llm_reservations, "record_model_call", ambiguous)
    await reservation.settle([], complete=False)
    assert (await budget.check_budget(job_id=jobs[0], provider_id="anthropic")).ok
    monkeypatch.setattr(llm_reservations, "record_model_call", writer)
    assert (await budget.check_budget(job_id=jobs[0], provider_id="anthropic")).ok
    assert len(await rows(test_engine, jobs[0])) == 1


@pytest.mark.parametrize(
    "limit,field",
    [
        ("project_total", "max_project_spend_total_usd"),
        ("project_24h", "max_project_spend_24h_usd"),
    ],
)
async def test_project_limits_include_other_job_reservations(
    test_engine: AsyncEngine, jobs, limit, field
):
    await set_admin_settings_row(test_engine, **{field: 0.5})
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    verdict, refused = await reserve(jobs[1])
    assert verdict.limit == limit and refused is None
    await reservation.cancel()


async def test_concurrent_admissions_include_prospective_amount(test_engine: AsyncEngine, jobs):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.5)
    results = await asyncio.wait_for(
        asyncio.gather(*(reserve(jobs[0]) for _ in range(5))), timeout=10
    )
    assert sum(verdict.ok for verdict, _ in results) == 1
    for _, reservation in results:
        if reservation is not None:
            await reservation.cancel()


async def test_cancel_releases_unattempted_reservation(test_engine: AsyncEngine, jobs):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    await reservation.cancel()
    await reservation.cancel()
    assert (await budget.check_budget(job_id=jobs[0], provider_id="anthropic")).ok
    assert await rows(test_engine, jobs[0]) == []


@pytest.mark.parametrize("field", ["max_project_spend_total_usd", "max_project_spend_24h_usd"])
async def test_deleted_job_closes_reservation(test_engine: AsyncEngine, jobs, field):
    await set_admin_settings_row(test_engine, **{field: 0.5})
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    async with test_engine.begin() as conn:
        await conn.execute(delete(Job).where(Job.id == jobs[0]))
    await reservation.settle([], complete=False)
    assert reservation._closed
    assert reservation._id not in llm_reservations._outstanding
    assert await rows(test_engine, jobs[0]) == []
    verdict, next_call = await reserve(jobs[1])
    assert verdict.ok and next_call is not None
    await next_call.cancel()


async def test_record_model_call_idempotent_retry(test_engine: AsyncEngine, jobs):
    record_id = uuid4()
    for _ in range(2):
        assert await llm_metering.record_model_call(
            job_id=str(jobs[0]),
            kind="discovery",
            model=MODEL,
            provider_id="anthropic",
            buckets=ESTIMATE,
            record_id=record_id,
        )
    assert len(await rows(test_engine, jobs[0])) == 1


async def test_recent_limit_ignores_old_durable_cost(test_engine: AsyncEngine, jobs):
    await set_admin_settings_row(test_engine, max_project_spend_24h_usd=0.5)
    async with AsyncSession(test_engine) as session:
        session.add(
            CostRecord(
                job_id=jobs[1],
                operation_type="discovery",
                provider="anthropic",
                model=MODEL,
                input_tokens=1000,
                output_tokens=0,
                cost_usd=1.0,
                created_at=datetime.now(UTC) - timedelta(hours=25),
            )
        )
        await session.commit()
    verdict, reservation = await reserve(jobs[0])
    assert verdict.ok and reservation is not None
    await reservation.cancel()


async def test_cancelled_admission_leaves_no_reservation(
    test_engine: AsyncEngine, jobs, monkeypatch
):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    with monkeypatch.context() as patcher:
        patcher.setattr(budget, "_ledger_totals", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await reserve(jobs[0])
    verdict, reservation = await reserve(jobs[0])
    assert verdict.ok and reservation is not None
    await reservation.cancel()


async def test_pending_cost_adds_to_provider_billed_baseline(
    test_engine: AsyncEngine, jobs, monkeypatch
):
    from openscientist.providers.base import CostInfo

    await set_admin_settings_row(test_engine, max_project_spend_total_usd=0.6)
    monkeypatch.setattr(
        budget,
        "_provider_billing",
        AsyncMock(
            return_value=CostInfo(
                provider_name="anthropic",
                total_spend_usd=0.4,
                recent_spend_usd=0.0,
                recent_period_hours=24,
            )
        ),
    )
    verdict, reservation = await reserve(jobs[0])
    assert verdict.limit == "project_total" and reservation is None


@pytest.mark.parametrize("unrelated_spend", [0.0, 0.2])
async def test_budget_check_includes_other_job_debt(
    test_engine: AsyncEngine, jobs, monkeypatch, unrelated_spend
):
    async with AsyncSession(test_engine) as session:
        session.add(
            CostRecord(
                job_id=jobs[1],
                operation_type="discovery",
                provider="anthropic",
                model=MODEL,
                input_tokens=0,
                output_tokens=0,
                cost_usd=unrelated_spend,
            )
        )
        await session.commit()
    baseline = (await budget.project_spend()).last_24h
    await set_admin_settings_row(test_engine, max_project_spend_24h_usd=baseline + 0.3)
    verdict, reservation = await reserve(jobs[0])
    assert reservation is not None, verdict
    with monkeypatch.context() as patcher:
        patcher.setattr(llm_reservations, "record_model_call", AsyncMock(return_value=False))
        await reservation.settle([], complete=False)
        verdict = await budget.check_budget(job_id=jobs[1], provider_id="anthropic")
        assert verdict.limit == "project_24h"
    assert (await budget.project_spend()).last_24h == pytest.approx(baseline + 0.3)
    assert len(await rows(test_engine, jobs[0])) == 1


async def test_uuid_allocation_failure_retains_debt(test_engine: AsyncEngine, jobs, monkeypatch):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    with monkeypatch.context() as patcher:

        def unavailable():
            raise OSError("database unavailable")

        patcher.setattr(llm_reservations, "get_admin_session", unavailable)
        await reservation.settle([], complete=False)
        assert (
            await budget.check_budget(job_id=jobs[0], provider_id="anthropic")
        ).limit == "job_cap"
    assert (await budget.check_budget(job_id=jobs[0], provider_id="anthropic")).limit == "job_cap"
    assert len(await rows(test_engine, jobs[0])) == 1


async def test_actual_rows_preserve_uuid7_order(test_engine: AsyncEngine, jobs):
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    await reservation.settle(
        [
            _ObservedCall(MODEL, TokenUsage(input_tokens=10)),
            _ObservedCall(MODEL, TokenUsage(input_tokens=20)),
        ],
        complete=True,
    )
    recorded = sorted(await rows(test_engine, jobs[0]), key=lambda row: row.id)
    assert [row.input_tokens for row in recorded] == [10, 20]
    assert all(row.id.version == 7 for row in recorded)


async def test_cancelled_settlement_is_retried_by_budget_check(test_engine, jobs, monkeypatch):
    _, reservation = await reserve(jobs[0])
    assert reservation is not None
    entered = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def blocked_guard():
        entered.set()
        await release.wait()
        yield

    with monkeypatch.context() as patch:
        patch.setattr(llm_reservations, "_budget_guard", blocked_guard)
        task = asyncio.create_task(reservation.settle([], complete=False))
        async with asyncio.timeout(5):
            await entered.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await budget.check_budget(job_id=jobs[0], provider_id="anthropic")
    recorded = await rows(test_engine, jobs[0])
    assert len(recorded) == 1
    assert recorded[0].operation_type.endswith("_estimated")

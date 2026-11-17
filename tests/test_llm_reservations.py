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
from openscientist.llm_reservations import (
    ModelCallRefused,
    ModelCallReservation,
    reserve_model_call,
)
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
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    await reservation.settle([], complete=False)
    verdict = await budget.check_budget(job_id=jobs[0], provider_id="anthropic")
    assert verdict.limit == "job_cap"
    recorded = await rows(test_engine, jobs[0])
    assert len(recorded) == 1
    assert recorded[0].cost_usd == pytest.approx(0.3)
    assert recorded[0].operation_type == "discovery_estimated"


async def test_missing_usage_retains_estimate(test_engine: AsyncEngine, jobs):
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    await reservation.settle(
        [_ObservedCall(MODEL, TokenUsage(), usage_reported=False)], complete=True
    )
    recorded = await rows(test_engine, jobs[0])
    assert recorded[0].cost_usd == pytest.approx(0.3)
    assert recorded[0].operation_type.endswith("_estimated")


async def test_reported_zero_usage_replaces_estimate(test_engine: AsyncEngine, jobs):
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
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
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    await reservation.settle([_ObservedCall(MODEL, TokenUsage(input_tokens=50))], complete=True)
    await reservation.settle([], complete=False)
    next_reservation = await reserve(jobs[0])
    assert isinstance(next_reservation, ModelCallReservation)
    await next_reservation.cancel()
    recorded = await rows(test_engine, jobs[0])
    assert len(recorded) == 1
    assert recorded[0].cost_usd == pytest.approx(0.05)
    assert recorded[0].operation_type == "discovery"


@pytest.mark.parametrize("observed,expected", [(10, 0.3), (400, 0.4)])
async def test_partial_usage_is_conservative(test_engine: AsyncEngine, jobs, observed, expected):
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    await reservation.settle(
        [_ObservedCall(MODEL, TokenUsage(input_tokens=observed))], complete=False
    )
    recorded = await rows(test_engine, jobs[0])
    assert recorded[0].cost_usd == pytest.approx(expected)
    assert recorded[0].operation_type.endswith("_estimated")


async def test_failed_write_counts_and_retries_once(test_engine: AsyncEngine, jobs, monkeypatch):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
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
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
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
        ("project_month", "max_project_spend_month_usd"),
        ("project_24h", "max_project_spend_24h_usd"),
    ],
)
async def test_project_limits_include_other_job_reservations(
    test_engine: AsyncEngine, jobs, limit, field
):
    _, total, recent = await budget._ledger_totals()
    baseline = total if limit == "project_month" else recent
    # The first reservation leaves the project under the limit, the second has
    # to count it to see the limit reached.
    await set_admin_settings_row(test_engine, **{field: baseline + 0.25})
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    refused = await reserve(jobs[1])
    assert isinstance(refused, ModelCallRefused)
    assert refused.limit == limit
    await reservation.cancel()


async def test_concurrent_admissions_count_earlier_reservations(test_engine: AsyncEngine, jobs):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.25)
    results = await asyncio.wait_for(
        asyncio.gather(*(reserve(jobs[0]) for _ in range(5))), timeout=10
    )
    assert sum(isinstance(result, ModelCallReservation) for result in results) == 1
    for reservation in results:
        if isinstance(reservation, ModelCallReservation):
            await reservation.cancel()


async def test_a_job_under_its_cap_is_admitted_whatever_the_call_could_cost(
    test_engine: AsyncEngine, jobs
):
    """$0.30 of a $0.31 cap is left, and the call's worst case is $30."""
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.31)
    spent = await reserve(jobs[0])
    assert isinstance(spent, ModelCallReservation)
    await spent.settle([], complete=False)
    large = await reserve_model_call(
        job_id=str(jobs[0]),
        kind="discovery",
        model=MODEL,
        provider_id="anthropic",
        buckets=TokenUsage(input_tokens=10_000, output_tokens=10_000),
    )
    assert isinstance(large, ModelCallReservation)
    await large.cancel()


async def test_the_refusal_names_the_spend_and_the_calls_in_flight(test_engine: AsyncEngine, jobs):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.25)
    in_flight = await reserve(jobs[0])
    assert isinstance(in_flight, ModelCallReservation)
    refused = await reserve(jobs[0])
    assert isinstance(refused, ModelCallRefused)
    assert refused.message == (
        "Job cost limit of $0.25 reached: the job has spent $0.00, "
        "with $0.30 more in model calls still running."
    )
    await in_flight.cancel()


async def test_report_calls_pass_the_job_cap_but_not_the_project_limits(
    test_engine: AsyncEngine, jobs
):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    spent = await reserve(jobs[0])
    assert isinstance(spent, ModelCallReservation)
    await spent.settle([], complete=False)
    assert isinstance(await reserve(jobs[0]), ModelCallRefused)

    async with AsyncSession(test_engine) as session:
        job = await session.get(Job, jobs[0])
        assert job is not None
        job.status = "generating_report"
        await session.commit()
    report = await reserve(jobs[0])
    assert isinstance(report, ModelCallReservation)
    await report.cancel()

    _, month, _ = await budget._ledger_totals()
    await set_admin_settings_row(
        test_engine, max_job_cost_usd=0.3, max_project_spend_month_usd=month
    )
    refused = await reserve(jobs[0])
    assert isinstance(refused, ModelCallRefused)
    assert refused.limit == "project_month"


async def test_cancel_releases_unattempted_reservation(test_engine: AsyncEngine, jobs):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    await reservation.cancel()
    await reservation.cancel()
    assert (await budget.check_budget(job_id=jobs[0], provider_id="anthropic")).ok
    assert await rows(test_engine, jobs[0]) == []


async def unattributed_rows(engine: AsyncEngine) -> list[CostRecord]:
    """The cost rows of this file's model whose job no longer exists."""
    async with AsyncSession(engine) as session:
        return list(
            (
                await session.scalars(
                    select(CostRecord).where(CostRecord.job_id.is_(None), CostRecord.model == MODEL)
                )
            ).all()
        )


@pytest_asyncio.fixture
async def no_unattributed_rows(test_engine: AsyncEngine) -> AsyncIterator[None]:
    """Remove the cost rows a test leaves for a deleted job."""
    yield
    async with test_engine.begin() as conn:
        await conn.execute(
            delete(CostRecord).where(CostRecord.job_id.is_(None), CostRecord.model == MODEL)
        )


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("max_project_spend_month_usd", "project_month"),
        ("max_project_spend_24h_usd", "project_24h"),
    ],
)
async def test_a_call_settled_after_its_job_is_deleted_still_counts_toward_the_project(
    test_engine: AsyncEngine, jobs, no_unattributed_rows: None, field: str, limit: str
):
    _, total, recent = await budget._ledger_totals()
    baseline = total if field == "max_project_spend_month_usd" else recent
    await set_admin_settings_row(test_engine, **{field: baseline + 0.25})
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    async with test_engine.begin() as conn:
        await conn.execute(delete(Job).where(Job.id == jobs[0]))
    await reservation.settle([], complete=False)
    assert reservation._closed
    assert reservation._id not in llm_reservations._outstanding
    [row] = await unattributed_rows(test_engine)
    assert row.cost_usd == pytest.approx(0.3)
    assert row.operation_type == "discovery_estimated"
    # The 0.3 the deleted job spent puts the project over its 0.25 limit.
    refused = await reserve(jobs[1])
    assert isinstance(refused, ModelCallRefused)
    assert refused.limit == limit


async def test_an_unreserved_call_of_a_deleted_job_is_recorded_without_a_job(
    test_engine: AsyncEngine, no_unattributed_rows: None
):
    _, month_before, _ = await budget._ledger_totals()
    assert await llm_metering.record_model_call(
        job_id=str(uuid4()),
        kind="discovery",
        model=MODEL,
        provider_id="anthropic",
        buckets=TokenUsage(input_tokens=0, output_tokens=703),
    )
    [row] = await unattributed_rows(test_engine)
    assert (row.output_tokens, row.user_id, row.iteration) == (703, None, None)
    assert row.cost_usd == pytest.approx(1.406)
    _, month_after, _ = await budget._ledger_totals()
    assert month_after == pytest.approx(month_before + 1.406)


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
    _, _, baseline = await budget._ledger_totals()
    await set_admin_settings_row(test_engine, max_project_spend_24h_usd=baseline + 0.5)
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
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    await reservation.cancel()


async def test_cancelled_admission_leaves_no_reservation(
    test_engine: AsyncEngine, jobs, monkeypatch
):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    with monkeypatch.context() as patcher:
        patcher.setattr(budget, "_ledger_totals", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await reserve(jobs[0])
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    await reservation.cancel()


async def test_pending_cost_adds_to_provider_billed_baseline(
    test_engine: AsyncEngine, jobs, monkeypatch
):
    from openscientist.providers.base import CostInfo

    _, baseline, _ = await budget._ledger_totals()
    await set_admin_settings_row(test_engine, max_project_spend_month_usd=baseline + 0.6)
    monkeypatch.setattr(
        budget,
        "_provider_billing",
        AsyncMock(
            return_value=CostInfo(
                provider_name="anthropic",
                month_spend_usd=baseline + 0.4,
                recent_spend_usd=0.0,
                recent_period_hours=24,
            )
        ),
    )
    # Billed $0.40 plus the first call's $0.30 in flight reaches the limit,
    # which the ledger plus that call alone would not.
    in_flight = await reserve(jobs[0])
    assert isinstance(in_flight, ModelCallReservation)
    refused = await reserve(jobs[1])
    assert isinstance(refused, ModelCallRefused)
    assert refused.limit == "project_month"
    await in_flight.cancel()


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
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
    with monkeypatch.context() as patcher:
        patcher.setattr(llm_reservations, "record_model_call", AsyncMock(return_value=False))
        await reservation.settle([], complete=False)
        verdict = await budget.check_budget(job_id=jobs[1], provider_id="anthropic")
        assert verdict.limit == "project_24h"
    assert (await budget.project_spend()).last_24h == pytest.approx(baseline + 0.3)
    assert len(await rows(test_engine, jobs[0])) == 1


async def test_uuid_allocation_failure_retains_debt(test_engine: AsyncEngine, jobs, monkeypatch):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.3)
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
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
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
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
    reservation = await reserve(jobs[0])
    assert isinstance(reservation, ModelCallReservation)
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

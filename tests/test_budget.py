"""Behaviour tests for the web's budget enforcement against the ledger and billing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openscientist import budget
from openscientist.database.models import CostRecord, Job
from openscientist.exceptions import ProviderError
from openscientist.providers.base import CostInfo
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

UNPRICED_MODEL = "no-such-model-12345"
PRICED_MODEL = "claude-sonnet-4-6"


@pytest_asyncio.fixture(autouse=True)
async def admin_settings_row(test_engine: AsyncEngine):
    """The limits the check reads, reset to the defaults between tests."""
    await set_admin_settings_row(test_engine)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


@pytest.fixture(autouse=True)
def fresh_billing_cache():
    """The per-process billing cache starts empty, as a fresh web process would."""
    budget._billed = None
    budget._billed_at = 0.0
    yield
    budget._billed = None
    budget._billed_at = 0.0


@asynccontextmanager
async def committed(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session that really commits, so the app's own sessions see the rows."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


async def _job(session: AsyncSession, **overrides) -> Job:
    job = Job(research_question="Test?", **overrides)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


def _message(verdict: budget.BudgetVerdict, *needles: str) -> None:
    """A refused verdict names each needle in its message."""
    assert verdict.message is not None
    for needle in needles:
        assert needle in verdict.message


async def _cost(
    session: AsyncSession, job_id: UUID, cost: float, when: datetime | None = None
) -> None:
    record = CostRecord(
        job_id=job_id,
        operation_type="discovery",
        provider="anthropic",
        model=PRICED_MODEL,
        input_tokens=100,
        output_tokens=100,
        cost_usd=cost,
    )
    if when is not None:
        record.created_at = when
    session.add(record)
    await session.commit()


@asynccontextmanager
async def job_with_costs(
    engine: AsyncEngine, costs: list[tuple[float, datetime | None]]
) -> AsyncIterator[Job]:
    """A job and its ledger rows, deleted again so the deployment-wide sums
    of the next test start clean."""
    async with committed(engine) as session:
        job = await _job(session)
        for cost, when in costs:
            await _cost(session, job.id, cost, when=when)
        try:
            yield job
        finally:
            await session.execute(delete(Job).where(Job.id == job.id))
            await session.commit()


def _billing(fetches: list[object], total: float | None, recent: float | None) -> SimpleNamespace:
    """The provider billing boundary: a provider whose get_cost_info counts its calls."""
    info = CostInfo(
        provider_name="stub",
        total_spend_usd=total,
        recent_spend_usd=recent,
        recent_period_hours=24,
    )

    def get_cost_info(lookback_hours: int = 24) -> CostInfo:
        fetches.append(lookback_hours)
        return info

    return SimpleNamespace(id="anthropic", get_cost_info=get_cost_info)


def _billing_failing(fetches: list[object]) -> SimpleNamespace:
    def get_cost_info(lookback_hours: int = 24) -> CostInfo:
        fetches.append(None)
        raise ProviderError("the billing API is down")

    return SimpleNamespace(id="anthropic", get_cost_info=get_cost_info)


class TestUnpricedModel:
    async def test_a_limit_set_refuses_an_unpriced_cloud_model(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=1000.0)
        verdict = await budget.check_budget(
            provider_id="anthropic", model=UNPRICED_MODEL, include_unpriced=True
        )
        assert verdict.ok is False
        assert verdict.limit == "unpriced_model"
        _message(verdict, UNPRICED_MODEL)

    async def test_no_limit_set_allows_an_unpriced_cloud_model(
        self, test_engine: AsyncEngine
    ) -> None:
        verdict = await budget.check_budget(
            provider_id="anthropic", model=UNPRICED_MODEL, include_unpriced=True
        )
        assert verdict.ok is True
        assert verdict.limit is None

    @pytest.mark.parametrize("provider_id", ["ollama", "vllm", "llamacpp"])
    async def test_self_hosted_providers_are_exempt(
        self, test_engine: AsyncEngine, provider_id: str
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=1000.0)
        verdict = await budget.check_budget(
            provider_id=provider_id, model=UNPRICED_MODEL, include_unpriced=True
        )
        assert verdict.ok is True

    async def test_the_warning_threshold_counts_as_a_limit_set(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, warn_project_spend_24h_usd=50.0)
        verdict = await budget.check_budget(
            provider_id="bedrock", model=UNPRICED_MODEL, include_unpriced=True
        )
        assert verdict.ok is False
        assert verdict.limit == "unpriced_model"

    async def test_a_priced_model_is_allowed(self, test_engine: AsyncEngine) -> None:
        await set_admin_settings_row(test_engine, max_job_cost_usd=10.0)
        verdict = await budget.check_budget(
            provider_id="bedrock", model=PRICED_MODEL, include_unpriced=True
        )
        assert verdict.ok is True

    async def test_the_unpriced_check_is_skipped_when_asked_for(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=1000.0)
        verdict = await budget.check_budget(
            provider_id="anthropic", model=UNPRICED_MODEL, include_unpriced=False
        )
        assert verdict.ok is True


class TestJobCap:
    async def test_a_job_exactly_at_its_cap_is_refused(self, test_engine: AsyncEngine) -> None:
        await set_admin_settings_row(test_engine, max_job_cost_usd=10.0)
        async with job_with_costs(test_engine, [(4.0, None), (6.0, None)]) as job:
            verdict = await budget.check_budget(
                job_id=job.id,
                provider_id="bedrock",
                model=PRICED_MODEL,
                include_unpriced=True,
            )
        assert verdict.ok is False
        assert verdict.limit == "job_cap"
        _message(verdict, "Job cost limit", "$10.00")

    async def test_a_job_below_its_cap_is_allowed(self, test_engine: AsyncEngine) -> None:
        await set_admin_settings_row(test_engine, max_job_cost_usd=10.0)
        async with job_with_costs(test_engine, [(9.99, None)]) as job:
            verdict = await budget.check_budget(
                job_id=job.id, provider_id="bedrock", model=PRICED_MODEL
            )
        assert verdict.ok is True

    async def test_a_null_job_cap_is_no_limit(self, test_engine: AsyncEngine) -> None:
        async with job_with_costs(test_engine, [(1000.0, None)]) as job:
            verdict = await budget.check_budget(job_id=job.id, provider_id="bedrock")
        assert verdict.ok is True

    async def test_the_job_cap_counts_only_the_job_own_rows(self, test_engine: AsyncEngine) -> None:
        await set_admin_settings_row(test_engine, max_job_cost_usd=10.0)
        async with (
            job_with_costs(test_engine, [(100.0, None)]),
            job_with_costs(test_engine, [(2.0, None)]) as job,
        ):
            verdict = await budget.check_budget(job_id=job.id, provider_id="bedrock")
        assert verdict.ok is True


class TestProjectSpend:
    async def test_project_total_uses_the_higher_of_ledger_and_billed(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        fetches: list[object] = []
        with _billing_patch(fetches, total=500.0, recent=50.0):
            async with job_with_costs(test_engine, [(10.0, None)]):
                verdict = await budget.check_budget(provider_id="bedrock")
        assert verdict.ok is False
        assert verdict.limit == "project_total"
        _message(verdict, "$500.00")

    async def test_the_ledger_wins_when_it_is_higher_than_the_billing(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        fetches: list[object] = []
        with _billing_patch(fetches, total=10.0, recent=1.0):
            async with job_with_costs(test_engine, [(500.0, None)]):
                verdict = await budget.check_budget(provider_id="bedrock")
        assert verdict.ok is False
        assert verdict.limit == "project_total"
        _message(verdict, "$500.00")

    async def test_no_billed_total_counts_the_ledger_only(self, test_engine: AsyncEngine) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        fetches: list[object] = []
        with _billing_patch(fetches, total=None, recent=None):
            async with job_with_costs(test_engine, [(10.0, None)]):
                verdict = await budget.check_budget(provider_id="anthropic")
        assert verdict.ok is True

    async def test_project_total_exactly_at_the_limit_is_refused(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        async with job_with_costs(test_engine, [(100.0, None)]):
            verdict = await budget.check_budget(provider_id="bedrock")
        assert verdict.ok is False
        assert verdict.limit == "project_total"

    async def test_project_24h_counts_only_the_last_24_hours(
        self, test_engine: AsyncEngine
    ) -> None:
        old = datetime.now(UTC) - timedelta(hours=25)
        fetches: list[object] = []
        with _billing_patch(fetches, total=0.0, recent=0.0):
            async with job_with_costs(test_engine, [(500.0, old), (10.0, None)]):
                await set_admin_settings_row(test_engine, max_project_spend_24h_usd=50.0)
                # The 25-hour-old row is outside the window.
                verdict = await budget.check_budget(provider_id="bedrock")
                assert verdict.ok is True

                await set_admin_settings_row(test_engine, max_project_spend_24h_usd=5.0)
                verdict = await budget.check_budget(provider_id="bedrock")
        assert verdict.ok is False
        assert verdict.limit == "project_24h"

    async def test_a_null_project_limit_is_no_limit(self, test_engine: AsyncEngine) -> None:
        async with job_with_costs(test_engine, [(1000.0, None)]):
            verdict = await budget.check_budget(provider_id="bedrock")
        assert verdict.ok is True

    async def test_the_ledger_is_read_live_at_each_check(self, test_engine: AsyncEngine) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        fetches: list[object] = []
        with _billing_patch(fetches, total=10.0, recent=1.0):
            async with committed(test_engine) as session:
                job = await _job(session)
                try:
                    assert (await budget.check_budget(provider_id="bedrock")).ok is True
                    await _cost(session, job.id, 500.0)
                    verdict = await budget.check_budget(provider_id="bedrock")
                finally:
                    await session.execute(delete(Job).where(Job.id == job.id))
                    await session.commit()
        assert verdict.ok is False
        assert verdict.limit == "project_total"
        assert fetches == [24]  # the second check reuses the cached fetch


def _billing_patch(fetches: list[object], total: float | None, recent: float | None):
    return patch.object(budget, "get_provider", return_value=_billing(fetches, total, recent))


class _Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


class TestBillingFreshness:
    async def test_the_billed_total_is_reused_within_10_minutes(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100000.0)
        clock = _Clock()
        fetches: list[object] = []
        with (
            patch.object(budget, "time", SimpleNamespace(monotonic=clock.monotonic)),
            _billing_patch(fetches, total=10.0, recent=1.0),
        ):
            for _ in range(2):
                await budget.check_budget(provider_id="bedrock")
            assert fetches == [24]
            clock.now += 599
            await budget.check_budget(provider_id="bedrock")
            assert fetches == [24]
            clock.now += 2
            await budget.check_budget(provider_id="bedrock")
            assert fetches == [24, 24]

    async def test_a_failed_fetch_is_retried_on_the_next_check(
        self, test_engine: AsyncEngine
    ) -> None:
        """A failure is not cached for 10 minutes: every check fetches again."""
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        fetches: list[object] = []
        with patch.object(budget, "get_provider", return_value=_billing_failing(fetches)):
            async with job_with_costs(test_engine, [(10.0, None)]):
                for _ in range(3):
                    verdict = await budget.check_budget(provider_id="bedrock")
                    assert verdict.ok is True
        assert fetches == [None, None, None]

    async def test_a_failed_refresh_falls_back_to_the_ledger(
        self, test_engine: AsyncEngine
    ) -> None:
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        clock = _Clock()
        fetches: list[object] = []
        ok = _billing(fetches, total=10.0, recent=1.0)
        failing = _billing_failing(fetches)
        with (
            patch.object(budget, "time", SimpleNamespace(monotonic=clock.monotonic)),
            patch.object(budget, "get_provider", side_effect=[ok, failing, failing]),
        ):
            async with job_with_costs(test_engine, [(10.0, None)]):
                # The first fetch succeeds and is cached for 10 minutes.
                verdict = await budget.check_budget(provider_id="bedrock")
                assert verdict.ok is True
                assert fetches == [24]
                # Past the 10 minutes the refresh fails, so the check counts
                # the ledger only, and the next check refreshes again.
                clock.now += 601
                verdict = await budget.check_budget(provider_id="bedrock")
                assert verdict.ok is True
                verdict = await budget.check_budget(provider_id="bedrock")
                assert fetches == [24, None, None]

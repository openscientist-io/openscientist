"""The project spend limit counts the calendar month (UTC), on the ledger and
on every provider's billed spend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openscientist import budget
from openscientist.database.models import CostRecord, Job
from openscientist.providers.base import month_start
from openscientist.providers.bedrock import BedrockProvider
from openscientist.providers.cborg import CborgProvider
from openscientist.providers.vertex import VertexProvider
from openscientist.settings import clear_settings_cache
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

PRICED_MODEL = "claude-sonnet-4-6"


def test_the_month_starts_in_utc_for_a_time_given_in_another_zone() -> None:
    """00:30 on 1 December at UTC+2 is still 30 November in UTC."""
    local = datetime(2026, 12, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert month_start(local) == datetime(2026, 11, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def job_id(test_engine: AsyncEngine) -> AsyncIterator[UUID]:
    """A job whose ledger rows are removed again, so the deployment-wide sums
    of the next test start clean."""
    await set_admin_settings_row(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as session:
        job = Job(research_question="Monthly?")
        session.add(job)
        await session.commit()
    yield job.id
    async with maker() as session:
        await session.execute(delete(CostRecord).where(CostRecord.job_id == job.id))
        await session.execute(delete(Job).where(Job.id == job.id))
        await session.commit()
    await reset_admin_settings_row(test_engine)


async def _cost(engine: AsyncEngine, job_id: UUID, cost: float, when: datetime) -> None:
    async with AsyncSession(engine) as session:
        session.add(
            CostRecord(
                job_id=job_id,
                operation_type="discovery",
                provider="anthropic",
                model=PRICED_MODEL,
                input_tokens=100,
                output_tokens=100,
                cost_usd=cost,
                created_at=when,
            )
        )
        await session.commit()


def _no_billing() -> SimpleNamespace:
    return SimpleNamespace(
        id="anthropic",
        get_cost_info=lambda lookback_hours=24: budget.CostInfo.unavailable(
            "stub", "no billing", lookback_hours
        ),
    )


@pytest.mark.asyncio
async def test_last_months_spend_does_not_count_toward_this_months_limit(
    test_engine: AsyncEngine, job_id: UUID
) -> None:
    budget._billed = None
    _, month_before, _ = await budget._ledger_totals()
    this_month = month_start(datetime.now(UTC))
    await _cost(test_engine, job_id, 100.0, this_month - timedelta(hours=1))
    await _cost(test_engine, job_id, 3.0, this_month + timedelta(minutes=1))

    with patch.object(budget, "get_provider", return_value=_no_billing()):
        await set_admin_settings_row(test_engine, max_project_spend_month_usd=month_before + 50.0)
        allowed = await budget.check_budget(provider_id="anthropic")
        await set_admin_settings_row(test_engine, max_project_spend_month_usd=month_before + 2.0)
        refused = await budget.check_budget(provider_id="anthropic")

    assert allowed.ok is True
    assert refused.ok is False
    assert refused.limit == "project_month"
    assert refused.message is not None and "this calendar month (UTC)" in refused.message


class _FrozenDatetime(datetime):
    frozen: ClassVar[datetime]

    @classmethod
    def now(cls, tz: object = None) -> _FrozenDatetime:  # type: ignore[override]
        return cls.fromtimestamp(cls.frozen.timestamp(), tz=UTC)


def _bedrock_cost_explorer(now: datetime) -> tuple[MagicMock, list[dict[str, str]]]:
    periods: list[dict[str, str]] = []
    client = MagicMock()

    def get_cost_and_usage(**kwargs: dict[str, str]) -> dict[str, object]:
        periods.append(kwargs["TimePeriod"])
        return {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "7.5"}}}]}

    client.get_cost_and_usage.side_effect = get_cost_and_usage
    _FrozenDatetime.frozen = now
    return client, periods


@pytest.mark.parametrize(
    ("now", "month_spend", "month_periods"),
    [
        (
            datetime(2026, 11, 16, 12, tzinfo=UTC),
            7.5,
            [{"Start": "2026-11-01", "End": "2026-11-16"}],
        ),
        # Cost Explorer leaves the End day out, so on the 1st the month is still empty.
        (datetime(2026, 12, 1, 12, tzinfo=UTC), 0.0, []),
    ],
)
def test_bedrock_bills_from_the_start_of_the_month(
    now: datetime,
    month_spend: float,
    month_periods: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    clear_settings_cache()
    client, periods = _bedrock_cost_explorer(now)
    with (
        patch("openscientist.providers.bedrock.datetime", _FrozenDatetime),
        patch("boto3.client", return_value=client),
    ):
        info = BedrockProvider().get_cost_info(lookback_hours=24)
    clear_settings_cache()

    assert info.month_spend_usd == month_spend
    recent = {
        "Start": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        "End": now.strftime("%Y-%m-%d"),
    }
    assert periods == [*month_periods, recent]


def test_cborg_counts_the_month_from_daily_activity_since_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "cborg")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "cborg-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.cborg.lbl.gov")
    clear_settings_cache()
    calls: list[tuple[str, dict[str, str]]] = []

    def get(url: str, **kwargs: dict[str, str]) -> MagicMock:
        calls.append((url, kwargs.get("params", {})))
        response = MagicMock()
        if url.endswith("/key/info"):
            response.json.return_value = {"info": {"spend": 900.0, "max_budget": 1000.0}}
        else:
            response.json.return_value = {"data": [{"spend": 4.0}, {"spend": 1.5}]}
        return response

    with patch("openscientist.providers.cborg.requests.get", side_effect=get):
        info = CborgProvider().get_cost_info(lookback_hours=24)
    clear_settings_cache()

    assert info.month_spend_usd == 5.5
    assert info.budget_remaining_usd == 100.0
    month_call = calls[1][1]
    assert datetime.fromisoformat(month_call["start_date"]) == month_start(
        datetime.fromisoformat(month_call["end_date"])
    )


def test_vertex_sums_the_billing_export_from_the_start_of_the_month(tmp_path: Path) -> None:
    creds = tmp_path / "sa.json"
    creds.write_text("{}")
    settings = MagicMock()
    settings.provider.anthropic_vertex_project_id = "proj-123"
    settings.provider.google_application_credentials = str(creds)
    settings.provider.gcp_billing_account_id = "BILL-1234"
    bq_client = MagicMock()
    bq_client.query.return_value.result.return_value = [
        SimpleNamespace(month_cost=12.0, recent_cost=2.0)
    ]
    with (
        patch("openscientist.providers.vertex.get_settings", return_value=settings),
        patch("google.oauth2.service_account.Credentials.from_service_account_file"),
        patch("google.cloud.bigquery.Client", return_value=bq_client),
    ):
        info = VertexProvider().get_cost_info(lookback_hours=6)

    month_query = bq_client.query.call_args_list[0].args[0]
    expected = month_start(info.last_updated).isoformat()
    assert f"usage_start_time >= TIMESTAMP('{expected}')" in month_query
    assert info.month_spend_usd == 12.0

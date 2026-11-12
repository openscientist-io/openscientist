"""Tests for the billing panel's cost-record rendering.

The panel is the only reader of ``cost_records``, and it went stale once already:
it showed input and output while ``cost_usd`` priced all five buckets, so the
displayed cost per token was incoherent and the dominant bucket was invisible.
The buckets it surfaces are pinned here.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from nicegui import ui
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from openscientist.database.models import CostRecord
from openscientist.providers.base import CostInfo
from openscientist.webapp_components.pages import billing

_PRICED_BUCKETS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _record(
    *,
    iteration: int | None = None,
    operation_type: str = "discovery",
    **buckets: int,
) -> CostRecord:
    return CostRecord(
        id=uuid4(),
        job_id=uuid4(),
        created_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        iteration=iteration,
        operation_type=operation_type,
        provider="anthropic",
        model="claude-opus-4-6",
        cost_usd=21.75,
        **buckets,
    )


class _Scalars:
    def __init__(self, records: Sequence[CostRecord]) -> None:
        self._records = records

    def all(self) -> Sequence[CostRecord]:
        return self._records


class _Result:
    def __init__(self, records: Sequence[CostRecord]) -> None:
        self._records = records

    def scalars(self) -> _Scalars:
        return _Scalars(self._records)


class _Session:
    def __init__(self, records: Sequence[CostRecord]) -> None:
        self._records = records

    async def execute(self, _statement: object) -> _Result:
        return _Result(self._records)


@contextlib.asynccontextmanager
async def _admin_session(records: Sequence[CostRecord]) -> AsyncIterator[_Session]:
    yield _Session(records)


async def _render(
    records: Sequence[CostRecord],
) -> tuple[list[tuple[str, str | int, str]], MagicMock]:
    """Run the real render, capturing the badges and the table it builds."""
    badges: list[tuple[str, str | int, str]] = []
    table = MagicMock()
    with (
        patch.object(billing, "get_admin_session", lambda: _admin_session(records)),
        patch.object(billing, "render_stat_badges", lambda stats: badges.extend(stats)),
        patch.object(ui, "table", table),
    ):
        await billing._render_db_cost_section()
    return badges, table


async def test_totals_badges_cover_every_priced_bucket() -> None:
    """A badge row that stops at input and output cannot explain ``cost_usd``."""
    badges, _ = await _render(
        [
            _record(
                input_tokens=500_000,
                output_tokens=100_000,
                cache_read_tokens=2_000_000,
                cache_write_tokens=200_000,
                reasoning_tokens=40,
            ),
            _record(
                input_tokens=1,
                output_tokens=2,
                cache_read_tokens=3,
                cache_write_tokens=4,
                reasoning_tokens=5,
            ),
        ]
    )

    assert dict((label, value) for label, value, _color in badges) == {
        "Total Cost": "$43.5000",
        "Input Tokens": "500,001",
        "Output Tokens": "100,002",
        "Cache Read": "2,000,003",
        "Cache Write": "200,004",
        "Reasoning": "45",
    }


async def test_per_record_table_carries_every_priced_bucket() -> None:
    record = _record(
        input_tokens=500_000,
        output_tokens=100_000,
        cache_read_tokens=2_000_000,
        cache_write_tokens=200_000,
        reasoning_tokens=40,
    )

    _, table = await _render([record])

    kwargs = table.call_args.kwargs
    assert [column["field"] for column in kwargs["columns"]] == [
        "job_id",
        "iteration",
        "model",
        "provider",
        *_PRICED_BUCKETS,
        "cost_usd",
        "created_at",
    ]
    row = kwargs["rows"][0]
    assert [row[bucket] for bucket in _PRICED_BUCKETS] == [
        500_000,
        100_000,
        2_000_000,
        200_000,
        40,
    ]


async def test_the_iteration_column_names_the_phase_each_record_covers() -> None:
    """A per-iteration record shows its number, the report its name, and a
    whole-run record nothing, so the table explains where each cost went."""
    records = [
        _record(
            iteration=2,
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_write_tokens=4,
            reasoning_tokens=5,
        ),
        _record(
            operation_type="report",
            input_tokens=6,
            output_tokens=7,
            cache_read_tokens=8,
            cache_write_tokens=9,
            reasoning_tokens=10,
        ),
        _record(
            input_tokens=11,
            output_tokens=12,
            cache_read_tokens=13,
            cache_write_tokens=14,
            reasoning_tokens=15,
        ),
    ]

    _, table = await _render(records)

    rows = table.call_args.kwargs["rows"]
    assert [row["iteration"] for row in rows] == ["2", "report", ""]


def _panel_ui_mock() -> MagicMock:
    """A NiceGUI stand-in whose container elements serve as context managers."""

    def factory(*_args: object, **_kwargs: object) -> MagicMock:
        element = MagicMock()
        element.__enter__ = MagicMock(return_value=element)
        element.__exit__ = MagicMock(return_value=False)
        element.classes.return_value = element
        return element

    mock = MagicMock()
    for name in ("card", "column", "row", "element"):
        setattr(mock, name, factory)
    return mock


def _fake_provider(total: float | None, recent: float | None) -> SimpleNamespace:
    """The provider billing boundary the page reads its figures from."""
    info = CostInfo(
        provider_name="Fake",
        total_spend_usd=total,
        recent_spend_usd=recent,
        recent_period_hours=24,
    )
    return SimpleNamespace(
        id="fake", display_name="Fake", get_cost_info=lambda lookback_hours=24: info
    )


async def _ledger_job(engine: AsyncEngine, cost: float) -> UUID:
    """One deployment-wide ledger row; the test deletes the job when done."""
    from openscientist.database.models import Job

    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        job = Job(research_question="Q?")
        session.add(job)
        await session.commit()
        await session.refresh(job)
        session.add(
            CostRecord(
                job_id=job.id,
                operation_type="discovery",
                provider="anthropic",
                model="m",
                input_tokens=1,
                output_tokens=1,
                cost_usd=cost,
            )
        )
        await session.commit()
        return job.id


async def _delete_job(engine: AsyncEngine, job_id: UUID) -> None:
    from openscientist.database.models import Job

    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(delete(CostRecord).where(CostRecord.job_id == job_id))
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


async def _render_panel_labels(
    provider: SimpleNamespace,
) -> list[str]:
    """Render the real panel and collect the labels it builds."""
    from openscientist import budget

    mock_ui = _panel_ui_mock()
    with (
        patch.object(billing, "render_stat_badges", MagicMock()),
        patch.object(billing, "render_empty_state", MagicMock()),
        patch.object(billing, "get_provider", return_value=provider),
        patch.object(budget, "get_provider", return_value=provider),
        patch.object(billing, "ui", mock_ui),
    ):
        await billing.render_billing_panel()
    return [c.args[0] for c in mock_ui.label.call_args_list if c.args]


async def test_project_section_shows_the_enforcement_figures_and_verdict(
    test_engine: AsyncEngine,
) -> None:
    """The page shows the higher of the ledger and the billed spend, and the
    verdict the enforcement acts on."""
    from openscientist import budget
    from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

    budget._billed = None
    job_id = None
    try:
        job_id = await _ledger_job(test_engine, 10.0)
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=100.0)
        labels = await _render_panel_labels(_fake_provider(total=500.0, recent=50.0))
        assert "$500.00" in labels
        assert "$50.00" in labels
        assert "Budget Limit Reached" in labels
        assert any("Project spend total limit of $100.00" in label for label in labels)
        assert "Provider: Fake" in labels
    finally:
        budget._billed = None
        await reset_admin_settings_row(test_engine)
        if job_id is not None:
            await _delete_job(test_engine, job_id)


async def test_project_section_shows_the_24h_warning_card(test_engine: AsyncEngine) -> None:
    """At or above the warning threshold and below the hard 24 h limit, the
    page warns without refusing."""
    from openscientist import budget
    from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

    budget._billed = None
    job_id = None
    try:
        job_id = await _ledger_job(test_engine, 10.0)
        await set_admin_settings_row(
            test_engine, max_project_spend_24h_usd=100.0, warn_project_spend_24h_usd=40.0
        )
        labels = await _render_panel_labels(_fake_provider(total=60.0, recent=50.0))
        assert "Budget Warning" in labels
        assert any("warning threshold $40.00" in label for label in labels)
        assert "Budget Limit Reached" not in labels
    finally:
        budget._billed = None
        await reset_admin_settings_row(test_engine)
        if job_id is not None:
            await _delete_job(test_engine, job_id)


async def test_project_section_counts_the_ledger_when_the_billing_reports_none(
    test_engine: AsyncEngine,
) -> None:
    from openscientist import budget
    from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

    budget._billed = None
    job_id = None
    try:
        job_id = await _ledger_job(test_engine, 10.0)
        await set_admin_settings_row(test_engine)
        labels = await _render_panel_labels(_fake_provider(total=None, recent=None))
        assert "$10.00" in labels
        assert "Budget Limit Reached" not in labels
        assert "Budget Warning" not in labels
    finally:
        budget._billed = None
        await reset_admin_settings_row(test_engine)
        if job_id is not None:
            await _delete_job(test_engine, job_id)


async def test_a_chat_record_is_labelled_chat() -> None:
    record = _record(
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )
    record.operation_type = "chat"

    _, table = await _render([record])

    assert table.call_args.kwargs["rows"][0]["iteration"] == "chat"


async def test_estimated_records_show_operation_and_provenance() -> None:
    records = [
        _record(
            operation_type=f"{operation}_estimated",
            iteration=3 if operation == "discovery" else None,
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
        )
        for operation in ("discovery", "chat", "report")
    ]
    _, table = await _render(records)
    assert [row["iteration"] for row in table.call_args.kwargs["rows"]] == [
        "3 discovery estimated",
        "chat estimated",
        "report estimated",
    ]

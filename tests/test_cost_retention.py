"""Recorded expenditure survives deletion of its job."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from nicegui.client import Client
from nicegui.page import page
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import budget
from openscientist.database.models import CostRecord, Job, User
from openscientist.job.repository import delete_job
from openscientist.llm_metering import TokenUsage, record_model_call
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["project_total", "project_24h"])
@pytest.mark.parametrize("deletion", ["repository", "sql", "loaded_orm"])
async def test_deleting_charged_job_preserves_spend_and_exhausted_budget(
    test_engine: AsyncEngine, limit: str, deletion: str
) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"cost-{uuid4().hex}@ledger.test", name="Cost owner")
        db.add(owner)
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Durable expenditure", status="completed")
        db.add(job)
        await db.commit()
        job_id, owner_id = job.id, owner.id
    ids = []
    try:
        for _ in range(2):
            await record_model_call(
                job_id=str(job_id),
                kind="discovery",
                model="claude-sonnet-4-6",
                provider_id="anthropic",
                buckets=TokenUsage(input_tokens=1_000_000),
            )
        async with AsyncSession(test_engine) as db:
            records = list(await db.scalars(select(CostRecord).where(CostRecord.job_id == job_id)))
            assert len(records) == 2
            ids = [record.id for record in records]
            await db.execute(
                update(CostRecord)
                .where(CostRecord.id == ids[0])
                .values(created_at=datetime.now(UTC) - timedelta(days=2))
            )
            await db.commit()
        with patch("openscientist.budget._provider_billing", return_value=None):
            before = await budget.project_spend()
            setting = (
                "max_project_spend_total_usd"
                if limit == "project_total"
                else "max_project_spend_24h_usd"
            )
            cap = before.total if limit == "project_total" else before.last_24h
            assert cap > 0
            await set_admin_settings_row(test_engine, **{setting: cap})
            assert (await budget.check_budget(provider_id="anthropic")).limit == limit
            if deletion == "repository":
                await delete_job(str(job_id), owner_id)
            else:
                async with AsyncSession(test_engine) as db:
                    if deletion == "sql":
                        await db.execute(delete(Job).where(Job.id == job_id))
                    else:
                        from sqlalchemy.orm import selectinload

                        loaded = await db.scalar(
                            select(Job)
                            .where(Job.id == job_id)
                            .options(selectinload(Job.cost_records))
                        )
                        assert loaded is not None
                        await db.delete(loaded)
                    await db.commit()
            after = await budget.project_spend()
            assert after.ledger_total == before.ledger_total
            assert after.ledger_24h == before.ledger_24h
            assert (await budget.check_budget(provider_id="anthropic")).limit == limit
            async with AsyncSession(test_engine) as db:
                retained = list(await db.scalars(select(CostRecord).where(CostRecord.id.in_(ids))))
                assert len(retained) == 2
                assert all(record.job_id is None for record in retained)
                assert all(record.user_id == owner_id for record in retained)
                assert all(record.provider == "anthropic" for record in retained)
    finally:
        await reset_admin_settings_row(test_engine)
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(CostRecord).where(CostRecord.id.in_(ids)))
            await db.execute(delete(Job).where(Job.id == job_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()


@pytest.mark.asyncio
async def test_admin_billing_keeps_user_total_after_job_deletion(test_engine: AsyncEngine) -> None:
    from unittest.mock import AsyncMock

    from openscientist.webapp_components.pages import billing

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"billing-{uuid4().hex}@ledger.test", name="Ledger reader")
        db.add(user)
        await db.flush()
        job = Job(owner_id=user.id, research_question="User total", status="completed")
        db.add(job)
        await db.commit()
        user_id, job_id = user.id, job.id
    ids = []
    try:
        await record_model_call(
            job_id=str(job_id),
            kind="chat",
            model="claude-sonnet-4-6",
            provider_id="anthropic",
            buckets=TokenUsage(input_tokens=1_000_000),
        )
        async with AsyncSession(test_engine) as db:
            ids = list(await db.scalars(select(CostRecord.id).where(CostRecord.job_id == job_id)))
        await delete_job(str(job_id), user_id)
        client = Client(page("/"))
        try:
            with (
                client,
                patch.object(billing, "ui") as widgets,
                patch.object(billing, "_render_project_budget_section", new_callable=AsyncMock),
            ):
                await billing.render_billing_panel()
        finally:
            client.delete()
        rows = [row for call in widgets.table.call_args_list for row in call.kwargs["rows"]]
        assert any(
            row.get("user_id") == str(user_id) and row.get("cost_usd") == "$3.0000" for row in rows
        )
        assert any(row.get("job_id") == "Deleted job" for row in rows)
    finally:
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(CostRecord).where(CostRecord.id.in_(ids)))
            await db.execute(delete(Job).where(Job.id == job_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.asyncio
async def test_retained_costs_keep_owner_visibility_and_survive_user_deletion(
    db_session: AsyncSession, test_user: User, test_user2: User
) -> None:
    from sqlalchemy import func, text
    from sqlalchemy.exc import DBAPIError

    from openscientist.database.models import JobShare
    from openscientist.database.rls import set_current_user

    job = Job(owner_id=test_user.id, research_question="Ledger permissions")
    db_session.add(job)
    await db_session.flush()
    record = CostRecord(
        job_id=job.id,
        user_id=test_user.id,
        operation_type="chat",
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=0,
        cost_usd=3.0,
    )
    db_session.add_all(
        [
            record,
            JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="view"),
        ]
    )
    await db_session.commit()
    job_id, cost_id, owner_id, reader_id = job.id, record.id, test_user.id, test_user2.id
    for caller in (owner_id, reader_id):
        async with db_session.begin_nested():
            await db_session.execute(text("SET LOCAL ROLE openscientist_app"))
            await set_current_user(db_session, caller)
            assert await budget.job_spends(db_session, [job_id]) == {job_id: 3.0}
            await db_session.execute(text("SET LOCAL ROLE openscientist_admin"))
    for statement in (
        delete(CostRecord).where(CostRecord.id == cost_id),
        update(CostRecord).where(CostRecord.id == cost_id).values(cost_usd=0.0),
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db_session.begin_nested():
                await db_session.execute(text("SET LOCAL ROLE openscientist_app"))
                await set_current_user(db_session, owner_id)
                await db_session.execute(statement)
    await db_session.execute(delete(Job).where(Job.id == job_id))
    for caller, expected in ((owner_id, cost_id), (reader_id, None)):
        async with db_session.begin_nested():
            await db_session.execute(text("SET LOCAL ROLE openscientist_app"))
            await set_current_user(db_session, caller)
            assert (
                await db_session.scalar(select(CostRecord.id).where(CostRecord.id == cost_id))
                == expected
            )
            await db_session.execute(text("SET LOCAL ROLE openscientist_admin"))
    await db_session.delete(test_user)
    await db_session.flush()
    assert (
        await db_session.scalar(
            select(func.sum(CostRecord.cost_usd)).where(CostRecord.id == cost_id)
        )
        == 3.0
    )
    assert (
        await db_session.scalar(select(CostRecord.user_id).where(CostRecord.id == cost_id)) is None
    )

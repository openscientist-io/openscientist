from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.api.endpoints import jobs
from openscientist.database.models import Administrator, Job, User
from openscientist.llm_proxy import _budget_refusal_body
from openscientist.ntfy import notify_job_budget_hold, notify_job_completed, notify_job_failed
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.test_budget import committed


@pytest.fixture(autouse=True)
async def reset_visibility(test_engine: AsyncEngine) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


async def test_an_admin_sees_amounts_through_the_web_role_before_any_user_is_set(
    db_session_rls: AsyncSession, test_engine: AsyncEngine
) -> None:
    """The API's refusal path asks before anything sets the current user, and
    the web role sees no administrator row until one is set."""
    await set_admin_settings_row(test_engine, show_job_costs=False)
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(email=f"admin-{uuid4().hex}@costs.test", name="Admin", is_approved=True)
        db.add(admin)
        await db.flush()
        db.add(Administrator(user_id=admin.id))
        await db.commit()
    try:
        assert await jobs._viewer_costs_visible(db_session_rls, admin)
    finally:
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(Administrator).where(Administrator.user_id == admin.id))
            await db.execute(delete(User).where(User.id == admin.id))
            await db.commit()


@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("administrator", [False, True])
async def test_job_responses_hide_budget_amounts(
    db_session: AsyncSession, test_engine: AsyncEngine, visible: bool, administrator: bool
) -> None:
    await set_admin_settings_row(test_engine, show_job_costs=visible)
    owner = User(email="cost-message@example.com", name="Cost reader", is_approved=True)
    db_session.add(owner)
    await db_session.flush()
    if administrator:
        db_session.add(Administrator(user_id=owner.id))
    reason = "Job cost limit of $5.00 reached."
    job = Job(
        owner_id=owner.id,
        research_question="Costs",
        status="pending",
        queue_reason=reason,
        stop_note=reason,
        error_message=reason,
    )
    db_session.add(job)
    await db_session.commit()
    detail = await jobs.get_job(str(job.id), owner, db_session)
    status = await jobs.get_job_status(str(job.id), owner, db_session)
    listing = await jobs.list_jobs(None, 100, 0, owner, db_session)
    expected = visible or administrator
    for response in (detail, status, listing.jobs[0]):
        assert ("$5.00" in (response.stop_note or "")) is expected
        assert "reached" in (response.stop_note or "")
        assert ("$5.00" in (response.queue_reason or "")) is expected
    assert ("$5.00" in (detail.error_message or "")) is expected
    assert ("$5.00" in (status.error_message or "")) is expected
    assert job.stop_note == reason


@pytest.mark.parametrize("visible", [False, True])
async def test_proxy_refusal_hides_transcript_amounts(
    test_engine: AsyncEngine, visible: bool
) -> None:
    await set_admin_settings_row(test_engine, show_job_costs=visible)
    body = await _budget_refusal_body("Job cost limit of $5.00 reached.")
    assert (b"$5.00" in body) is visible
    assert b"reached" in body
    assert b"budget_exceeded" in body


@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("administrator", [False, True])
@pytest.mark.parametrize("notification", ["hold", "completed", "failed"])
async def test_notifications_hide_budget_amounts(
    test_engine: AsyncEngine, visible: bool, administrator: bool, notification: str
) -> None:
    await set_admin_settings_row(test_engine, show_job_costs=visible)
    topic = f"cost-test-{uuid4()}"
    async with committed(test_engine) as session:
        owner = User(email=f"{topic}@example.com", name="Cost reader", ntfy_topic=topic)
        session.add(owner)
        await session.flush()
        if administrator:
            session.add(Administrator(user_id=owner.id))
        await session.commit()
        try:
            reason = "Job cost limit of $5.00 reached."
            with patch(
                "openscientist.ntfy.send_notification", AsyncMock(return_value=True)
            ) as send:
                if notification == "hold":
                    await notify_job_budget_hold(
                        topic, "job", "Research", reason, "https://app.test"
                    )
                elif notification == "completed":
                    await notify_job_completed(topic, "job", "Research", "https://app.test", reason)
                else:
                    await notify_job_failed(topic, "job", "Research", reason, "https://app.test")
            message = send.call_args.kwargs["message"]
            assert ("$5.00" in message) is (visible or administrator)
            assert "reached" in message
        finally:
            await session.execute(delete(User).where(User.id == owner.id))
            await session.commit()

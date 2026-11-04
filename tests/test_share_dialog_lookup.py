"""The share dialog: exact-email lookup and sharing against the real database."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth import middleware
from openscientist.database.models import Job, JobShare, Session, User
from openscientist.webapp_components.components import dialogs as dialog_module
from tests.helpers import fake_admin_session


def _controller(
    job: Job, owner: User, email: str, permission: str = "view"
) -> dialog_module._ShareDialogController:
    """A share-dialog controller with the email field pre-filled."""
    email_input = MagicMock()
    email_input.value = email
    return dialog_module._ShareDialogController(
        job_id=str(job.id),
        shares_container=MagicMock(),
        public_switch=MagicMock(),
        email_input=email_input,
        selected_user_container=MagicMock(),
        share_action_row=MagicMock(),
        permission_select=MagicMock(value=permission),
    )


def _committed_session(test_engine: AsyncEngine) -> AsyncSession:
    """A committed-state session whose rows stay readable after the block ends."""
    return AsyncSession(test_engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_dialog_shares_with_the_looked_up_user(
    test_engine: AsyncEngine,
):
    """Add then Share resolves the typed email and creates the share row.

    The rows are committed on their own session, because the dialog's owner
    re-validation reads through the app's real logins, which cannot see a
    fixture transaction's rows."""
    suffix = uuid4().hex
    owner = User(email=f"owner-{suffix}@lookup.test", name="Lookup Owner")
    owner.is_approved = True
    target = User(email=f"target-{suffix}@lookup.test", name="Lookup Target")
    job = Job(
        owner_id=None,
        research_question=f"Share lookup job {suffix[:8]}",
        llm_provider="mock",
        llm_config={"model": "mock-model-v1"},
        status="pending",
    )
    async with _committed_session(test_engine) as db:
        db.add_all([owner, target])
        await db.flush()
        job.owner_id = owner.id
        db.add(job)
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()

    try:
        controller = _controller(job, owner, f" {target.email.upper()} ")
        page_ui = MagicMock()
        page_ui.context.client.request.cookies = {"session_token": str(login.id)}

        with (
            patch.object(dialog_module, "ui"),
            patch.object(dialog_module, "get_current_user_id", return_value=str(owner.id)),
            patch.object(dialog_module, "can_current_user_start_jobs", return_value=True),
            patch.object(middleware, "ui", page_ui),
        ):
            await controller.add_user()
            assert controller.selected_user_state["email"] == target.email
            assert controller.selected_user_state["name"] == target.name
            await controller.do_share()

        async with _committed_session(test_engine) as db:
            share = (
                await db.execute(
                    select(JobShare).where(
                        JobShare.job_id == job.id,
                        JobShare.shared_with_user_id == target.id,
                    )
                )
            ).scalar_one()
            assert share.permission_level == "view"
    finally:
        async with _committed_session(test_engine) as db:
            await db.execute(delete(Job).where(Job.id == job.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, target.id])))
            await db.commit()


@pytest.mark.asyncio
async def test_dialog_no_match_reports_no_account(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
    test_job: Job,
):
    """A typed email no account uses offers nothing and says so."""
    test_user.is_approved = True
    await db_session.commit()
    controller = _controller(test_job, test_user, "nobody@example.invalid")

    with (
        patch.object(dialog_module, "ui") as mock_ui,
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
        patch.object(dialog_module, "can_current_user_start_jobs", return_value=True),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
    ):
        await controller.add_user()

    assert controller.selected_user_state["email"] is None
    mock_ui.notify.assert_called_once_with("No account uses that email", type="warning")
    assert (
        await db_session.execute(select(JobShare).where(JobShare.job_id == test_job.id))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_dialog_unapproved_caller_gets_no_target_offer(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
    test_job: Job,
):
    """An unapproved account is not offered a share target, even for its own jobs."""
    controller = _controller(test_job, test_user, test_user2.email)

    with (
        patch.object(dialog_module, "ui"),
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
        patch.object(dialog_module, "can_current_user_start_jobs", return_value=False),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
    ):
        await controller.add_user()

    assert controller.selected_user_state["email"] is None
    assert (
        await db_session.execute(select(JobShare).where(JobShare.job_id == test_job.id))
    ).scalars().all() == []

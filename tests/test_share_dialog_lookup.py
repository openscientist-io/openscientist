"""The share dialog: exact-email lookup and sharing against the real database."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, JobShare, User
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


@pytest.mark.asyncio
async def test_dialog_shares_with_the_looked_up_user(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
    test_job: Job,
):
    """Add then Share resolves the typed email and creates the share row."""
    test_user.is_approved = True
    await db_session.commit()
    controller = _controller(test_job, test_user, f" {test_user2.email.upper()} ")

    with (
        patch.object(dialog_module, "ui"),
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
        patch.object(dialog_module, "can_current_user_start_jobs", return_value=True),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
    ):
        await controller.add_user()
        assert controller.selected_user_state["email"] == test_user2.email
        assert controller.selected_user_state["name"] == test_user2.name
        await controller.do_share()

    share = (
        await db_session.execute(
            select(JobShare).where(
                JobShare.job_id == test_job.id,
                JobShare.shared_with_user_id == test_user2.id,
            )
        )
    ).scalar_one()
    assert share.permission_level == "view"


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

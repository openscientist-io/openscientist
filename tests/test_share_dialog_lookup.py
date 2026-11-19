"""The share dialog: exact-email lookup and sharing against the real database.

The rows are committed on their own session, because the dialog re-reads the
caller's session and approval through the app's real logins, which cannot
see a fixture transaction's rows."""

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth import middleware
from openscientist.database.models import Job, JobShare, Session, User
from openscientist.webapp_components.components import dialogs as dialog_module
from openscientist.webapp_components.utils import client_guard
from tests.page_request import page_request_storage


@dataclass(frozen=True)
class ShareWorld:
    """An approved owner signed in through ``login_token``, a job they own and
    an active user to share it with."""

    owner: User
    target: User
    job: Job
    login_token: str


@pytest_asyncio.fixture
async def share_world(test_engine: AsyncEngine) -> AsyncIterator[ShareWorld]:
    """The committed rows of one share-dialog scenario, removed afterwards."""
    suffix = uuid4().hex
    owner = User(email=f"owner-{suffix}@lookup.test", name="Lookup Owner", is_approved=True)
    target = User(email=f"target-{suffix}@lookup.test", name="Lookup Target")
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        db.add_all([owner, target])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question=f"Share lookup job {suffix[:8]}",
            llm_provider="mock",
            llm_config={"model": "mock-model-v1"},
            status="pending",
        )
        db.add(job)
        login, login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(login)
        await db.commit()
    yield ShareWorld(owner=owner, target=target, job=job, login_token=login_token)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, target.id])))
        await db.commit()


def _controller(world: ShareWorld, email: str) -> dialog_module._ShareDialogController:
    """A share-dialog controller with the email field pre-filled."""
    email_input = MagicMock()
    email_input.value = email
    return dialog_module._ShareDialogController(
        job_id=str(world.job.id),
        shares_container=MagicMock(),
        public_switch=MagicMock(),
        email_input=email_input,
        selected_user_container=MagicMock(),
        share_action_row=MagicMock(),
        permission_select=MagicMock(value="view"),
    )


@contextmanager
def _owner_page(world: ShareWorld) -> Iterator[MagicMock]:
    """The owner's open job page, built while they were approved: their login
    cookie on the page request, the page-build flags in user storage, and the
    dialog's notifications captured on the returned mock."""
    page_ui = MagicMock()
    page_ui.context.client.request.cookies = {"session_token": world.login_token}
    page_app = MagicMock()
    page_app.storage.user = {
        "user_id": str(world.owner.id),
        "is_approved": True,
        "can_start_jobs": True,
    }
    with (
        patch.object(client_guard, "ui"),
        patch.object(dialog_module, "ui") as dialog_ui,
        patch.object(dialog_module, "get_current_user_id", return_value=str(world.owner.id)),
        patch.object(middleware, "ui", page_ui),
        patch.object(middleware, "storage", page_request_storage(page_ui.context.client.request)),
        patch.object(middleware, "app", page_app),
    ):
        yield dialog_ui


async def _job_shares(test_engine: AsyncEngine, job: Job) -> list[JobShare]:
    """The share rows of ``job`` as committed."""
    async with AsyncSession(test_engine) as db:
        result = await db.execute(select(JobShare).where(JobShare.job_id == job.id))
        return list(result.scalars().all())


async def _remove_owner_approval(test_engine: AsyncEngine, owner: User) -> None:
    """Revoke the owner's approval, committed, while their page stays open."""
    async with AsyncSession(test_engine) as db:
        row = (await db.execute(select(User).where(User.id == owner.id))).scalar_one()
        row.is_approved = False
        await db.commit()


@pytest.mark.asyncio
async def test_dialog_shares_with_the_looked_up_user(
    share_world: ShareWorld,
    test_engine: AsyncEngine,
) -> None:
    """Add then Share resolves the typed email and creates the share row."""
    controller = _controller(share_world, f" {share_world.target.email.upper()} ")

    with _owner_page(share_world):
        await controller.add_user()
        assert controller.selected_user_state["email"] == share_world.target.email
        assert controller.selected_user_state["name"] == share_world.target.name
        await controller.do_share()

    shares = await _job_shares(test_engine, share_world.job)
    assert [(share.shared_with_user_id, share.permission_level) for share in shares] == [
        (share_world.target.id, "view")
    ]


@pytest.mark.asyncio
async def test_dialog_no_match_reports_no_account(
    share_world: ShareWorld,
    test_engine: AsyncEngine,
) -> None:
    """A typed email no account uses offers nothing and says so."""
    controller = _controller(share_world, "nobody@example.invalid")

    with _owner_page(share_world) as dialog_ui:
        await controller.add_user()

    assert controller.selected_user_state["email"] is None
    dialog_ui.notify.assert_called_once_with("No account uses that email", type="warning")
    assert await _job_shares(test_engine, share_world.job) == []


@pytest.mark.asyncio
async def test_dialog_lookup_refuses_an_owner_whose_approval_was_removed(
    share_world: ShareWorld,
    test_engine: AsyncEngine,
) -> None:
    """An owner whose approval is removed while the page stays open gets the
    same refusal for a known and an unknown email, and no target offer."""
    await _remove_owner_approval(test_engine, share_world.owner)
    notifications = []
    for email in (share_world.target.email, "nobody@example.invalid"):
        controller = _controller(share_world, email)
        with _owner_page(share_world) as dialog_ui:
            await controller.add_user()
        assert controller.selected_user_state["email"] is None
        notifications.append(dialog_ui.notify.call_args_list)

    assert notifications[0] == notifications[1]
    assert notifications[0] == [
        (("Your account is pending administrator approval",), {"type": "negative"})
    ]


@pytest.mark.asyncio
async def test_dialog_share_refuses_an_owner_whose_approval_was_removed(
    share_world: ShareWorld,
    test_engine: AsyncEngine,
) -> None:
    """A target selected while approved is not shared with once the approval
    is removed, and the refusal names no account."""
    controller = _controller(share_world, share_world.target.email)
    with _owner_page(share_world):
        await controller.add_user()
    assert controller.selected_user_state["email"] == share_world.target.email

    await _remove_owner_approval(test_engine, share_world.owner)
    with _owner_page(share_world) as dialog_ui:
        await controller.do_share()

    dialog_ui.notify.assert_called_once_with(
        "Your account is pending administrator approval", type="negative"
    )
    assert await _job_shares(test_engine, share_world.job) == []

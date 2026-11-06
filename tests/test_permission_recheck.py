"""Action-time permission rechecks on open pages (findings F2 and F8).

A page's authorization runs once while the page is built. These tests sign an
actor in, give the page's request the actor's login cookie, then demote the
actor or delete their session in the database, and require that the open
page's action handlers refuse and change nothing.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.admin_settings import current_admin_settings
from openscientist.auth import middleware
from openscientist.database.models import (
    Administrator,
    FeedbackHistory,
    Job,
    JobChatMessage,
    JobShare,
    ReviewToken,
    Session,
    User,
)
from openscientist.job.types import JobStatus
from openscientist.webapp_components.components import dialogs as dialogs_module
from openscientist.webapp_components.pages import admin as admin_page
from openscientist.webapp_components.pages import settings_panel
from openscientist.webapp_components.pages.job_detail import chat as chat_module
from openscientist.webapp_components.pages.job_detail import common as job_detail_common
from openscientist.webapp_components.pages.job_detail import feedback as feedback_module
from openscientist.webapp_components.pages.job_detail import iterations as iterations_module
from openscientist.webapp_components.pages.job_detail import report as report_module
from openscientist.webapp_components.pages.job_detail import timeline as timeline_module
from openscientist.webapp_components.utils import client_guard, permissions


async def _create_user(db: AsyncSession, name: str) -> User:
    user = User(email=f"{name.lower().replace(' ', '')}-{uuid4().hex}@recheck.test", name=name)
    db.add(user)
    await db.flush()
    return user


async def _create_job(db: AsyncSession, owner: User | None, status: str = "running") -> Job:
    job = Job(
        owner_id=owner.id if owner else None,
        research_question=f"Question {uuid4().hex[:8]}",
        llm_provider="mock",
        llm_config={"model": "mock-model-v1"},
        status=status,
    )
    db.add(job)
    await db.flush()
    return job


def _page_cookie(monkeypatch: pytest.MonkeyPatch, session_id: object) -> None:
    """Give the page's request the login cookie the actor signed in with."""
    page_ui = MagicMock()
    page_ui.context.client.request.cookies = {"session_token": str(session_id)}
    monkeypatch.setattr(middleware, "ui", page_ui)


def _refusal_ui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The ui the refusal notification lands in, captured for the assertion."""
    mock_ui = MagicMock()
    monkeypatch.setattr(permissions, "ui", mock_ui)
    return mock_ui


def _session(test_engine: AsyncEngine) -> AsyncSession:
    """A committed-state session whose rows stay readable after the block ends."""
    return AsyncSession(test_engine, expire_on_commit=False)


async def _noop() -> None:
    return None


async def _demote_admin(db: AsyncSession, user_id) -> None:
    await db.execute(delete(Administrator).where(Administrator.user_id == user_id))
    await db.commit()


async def _delete_session(db: AsyncSession, session_id) -> None:
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()


async def _purge(test_engine: AsyncEngine, *statements) -> None:
    """Delete rows a test committed, so the database is unchanged when it ends."""
    async with _session(test_engine) as db:
        for statement in statements:
            await db.execute(statement)
        await db.commit()


@pytest.fixture(autouse=True)
def _no_real_nicegui(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real NiceGUI ui out of these tests so no script client gets created."""
    monkeypatch.setattr(client_guard, "is_client_connected", lambda: True)
    monkeypatch.setattr(client_guard, "safe_run_javascript", lambda code: None)
    monkeypatch.setattr(chat_module, "safe_run_javascript", lambda code: None)
    monkeypatch.setattr(timeline_module, "is_client_connected", lambda: True)


@pytest_asyncio.fixture
async def admin_actor(test_engine: AsyncEngine):
    """A committed admin user with a live session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = await _create_user(db, "Recheck Admin")
        db.add(Administrator(user_id=user.id))
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
        yield {"user_id": user.id, "session_id": login.id}
    async with _session(test_engine) as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest_asyncio.fixture
async def job_actors(test_engine: AsyncEngine):
    """An owner, an edit-share editor, a view-share viewer and a target user,
    all signed in with committed sessions, sharing one awaiting-feedback job."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = await _create_user(db, "Recheck Owner")
        editor = await _create_user(db, "Recheck Editor")
        viewer = await _create_user(db, "Recheck Viewer")
        target = await _create_user(db, "Recheck Target")
        job = await _create_job(db, owner, status="awaiting_feedback")
        owner_login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        editor_login = Session(user_id=editor.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        viewer_login = Session(user_id=viewer.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add_all([owner_login, editor_login, viewer_login])
        db.add(JobShare(job_id=job.id, shared_with_user_id=editor.id, permission_level="edit"))
        db.add(JobShare(job_id=job.id, shared_with_user_id=viewer.id, permission_level="view"))
        await db.commit()
        actors = {
            "owner": {
                "user_id": owner.id,
                "session_id": owner_login.id,
            },
            "editor": {
                "user_id": editor.id,
                "session_id": editor_login.id,
            },
            "viewer": {
                "user_id": viewer.id,
                "session_id": viewer_login.id,
            },
            "target": target,
            "job_id": job.id,
        }
        yield actors
    async with _session(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(
            delete(User).where(User.id.in_([owner.id, editor.id, viewer.id, target.id]))
        )
        await db.commit()


@pytest_asyncio.fixture
async def victim_user(test_engine: AsyncEngine) -> AsyncGenerator[User, None]:
    """A committed user the tests refuse to change, deleted however the test ends."""
    async with _session(test_engine) as db:
        user = await _create_user(db, "Recheck Victim")
        await db.commit()
        yield user
    await _purge(test_engine, delete(User).where(User.id == user.id))


@pytest_asyncio.fixture
async def orphan_pair(test_engine: AsyncEngine) -> AsyncGenerator[tuple[User, Job], None]:
    """A committed recipient and an ownerless job, both deleted however the test ends."""
    async with _session(test_engine) as db:
        recipient = await _create_user(db, "Recheck Recipient")
        orphan = await _create_job(db, owner=None, status="completed")
        await db.commit()
        yield (recipient, orphan)
    await _purge(
        test_engine,
        delete(Job).where(Job.id == orphan.id),
        delete(User).where(User.id == recipient.id),
    )


@pytest_asyncio.fixture
async def review_token_row(
    test_engine: AsyncEngine, admin_actor
) -> AsyncGenerator[ReviewToken, None]:
    """A committed review token, deleted however the test ends."""
    async with _session(test_engine) as db:
        token = ReviewToken(
            token_hash=f"recheck-{uuid4().hex}",
            label="Recheck Reviewer",
            created_by_id=admin_actor["user_id"],
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(token)
        await db.commit()
        yield token
    await _purge(test_engine, delete(ReviewToken).where(ReviewToken.id == token.id))


async def _set_share_level(db: AsyncSession, job_id, user_id, level: str) -> None:
    await db.execute(
        update(JobShare)
        .where(JobShare.job_id == job_id, JobShare.shared_with_user_id == user_id)
        .values(permission_level=level)
    )
    await db.commit()


async def test_demoted_admin_cannot_toggle_user_approval(
    test_engine: AsyncEngine,
    admin_actor,
    victim_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)
    refusal = _refusal_ui(monkeypatch)

    async with _session(test_engine) as db:
        await _demote_admin(db, admin_actor["user_id"])

    event = SimpleNamespace(args={"id": str(victim_user.id)})

    async def reload_users() -> None:
        raise AssertionError("the table must not reload after a refused action")

    await admin_page._handle_approval_update(event, is_approved=True, reload_users=reload_users)

    async with _session(test_engine) as db:
        row = await db.get(User, victim_user.id)
        assert row is not None
        assert row.is_approved is False
    refusal.notify.assert_called()


async def test_admin_with_deleted_session_cannot_toggle_priority(
    test_engine: AsyncEngine,
    admin_actor,
    victim_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)
    refusal = _refusal_ui(monkeypatch)

    async with _session(test_engine) as db:
        await _delete_session(db, admin_actor["session_id"])

    event = SimpleNamespace(args={"id": str(victim_user.id)})

    async def reload_users() -> None:
        raise AssertionError("the table must not reload after a refused action")

    await admin_page._handle_priority_update(event, is_priority=True, reload_users=reload_users)

    async with _session(test_engine) as db:
        row = await db.get(User, victim_user.id)
        assert row is not None
        assert row.is_priority is False
    refusal.notify.assert_called()


async def test_admin_with_deleted_session_cannot_delete_user(
    test_engine: AsyncEngine,
    admin_actor,
    victim_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)
    refusal = _refusal_ui(monkeypatch)

    async with _session(test_engine) as db:
        await _delete_session(db, admin_actor["session_id"])

    async def reload_users() -> None:
        raise AssertionError("the table must not reload after a refused action")

    await admin_page._confirm_delete_user(
        str(victim_user.id), dialog=MagicMock(), on_deleted=reload_users
    )

    async with _session(test_engine) as db:
        assert await db.get(User, victim_user.id) is not None
    refusal.notify.assert_called()


async def test_live_admin_can_delete_user(
    test_engine: AsyncEngine,
    admin_actor,
    victim_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)

    await admin_page._confirm_delete_user(str(victim_user.id), dialog=MagicMock(), on_deleted=_noop)

    async with _session(test_engine) as db:
        assert await db.get(User, victim_user.id) is None


async def test_demoted_admin_cannot_assign_orphaned_job(
    test_engine: AsyncEngine,
    admin_actor,
    orphan_pair: tuple[User, Job],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)
    refusal = _refusal_ui(monkeypatch)
    recipient, orphan = orphan_pair

    async with _session(test_engine) as db:
        await _demote_admin(db, admin_actor["user_id"])

    await admin_page._confirm_assign_orphaned_job(
        str(orphan.id), str(recipient.id), dialog=MagicMock()
    )

    async with _session(test_engine) as db:
        row = await db.get(Job, orphan.id)
        assert row is not None
        assert row.owner_id is None
    refusal.notify.assert_called()


async def test_live_admin_can_assign_orphaned_job(
    test_engine: AsyncEngine,
    admin_actor,
    orphan_pair: tuple[User, Job],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)
    recipient, orphan = orphan_pair

    await admin_page._confirm_assign_orphaned_job(
        str(orphan.id), str(recipient.id), dialog=MagicMock()
    )

    async with _session(test_engine) as db:
        row = await db.get(Job, orphan.id)
        assert row is not None
        assert row.owner_id == recipient.id


async def test_demoted_admin_cannot_revoke_review_token(
    test_engine: AsyncEngine,
    admin_actor,
    review_token_row: ReviewToken,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)
    refusal = _refusal_ui(monkeypatch)
    token = review_token_row

    async with _session(test_engine) as db:
        await _demote_admin(db, admin_actor["user_id"])

    await admin_page._confirm_revoke_review_token(
        str(token.id), dialog=MagicMock(), on_revoked=_noop
    )

    async with _session(test_engine) as db:
        row = await db.get(ReviewToken, token.id)
        assert row is not None
        assert row.is_active is True
    refusal.notify.assert_called()


async def test_admin_with_deleted_session_cannot_create_review_token(
    test_engine: AsyncEngine, admin_actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    monkeypatch.setattr(admin_page, "get_current_user_id", lambda: str(admin_actor["user_id"]))
    mock_ui = MagicMock()
    monkeypatch.setattr(admin_page, "ui", mock_ui)
    refusal = _refusal_ui(monkeypatch)

    async with _session(test_engine) as db:
        await _delete_session(db, admin_actor["session_id"])

    label = f"Recheck Reviewer {uuid4().hex[:8]}"
    await admin_page._create_review_token(label, 30)

    async with _session(test_engine) as db:
        result = await db.execute(select(ReviewToken).where(ReviewToken.label == label))
        assert result.scalar_one_or_none() is None
    refusal.notify.assert_called()


async def test_admin_with_deleted_session_cannot_save_settings(
    test_engine: AsyncEngine, admin_actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    monkeypatch.setattr(settings_panel, "get_current_user_id", lambda: str(admin_actor["user_id"]))
    monkeypatch.setattr(settings_panel, "is_current_user_admin", lambda: True)
    mock_ui = MagicMock()
    monkeypatch.setattr(permissions, "ui", mock_ui)

    original_cpu = (await current_admin_settings()).agent_cpu
    async with _session(test_engine) as db:
        await _delete_session(db, admin_actor["session_id"])

    values = {"agent_cpu": float(original_cpu) + 1.0}

    success, _ = await settings_panel._save_settings(values)  # type: ignore[arg-type]

    assert success is False
    fresh = await current_admin_settings()
    assert fresh.agent_cpu == original_cpu
    mock_ui.notify.assert_called()


def _chat_context(job_actors: dict, actor: dict) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=str(job_actors["job_id"]),
        user_id=str(actor["user_id"]),
        job_info=SimpleNamespace(status=JobStatus.COMPLETED),
        job_dir=None,
        can_edit=True,
        is_owner=False,
        owner_name=None,
    )


def _fake_guard() -> type:
    class _Guard:
        is_connected = True

        def run_javascript(self, *args: object, **kwargs: object) -> None:
            return None

    return _Guard


async def test_demoted_editor_cannot_send_a_chat_turn(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = job_actors["editor"]
    _page_cookie(monkeypatch, editor["session_id"])
    monkeypatch.setattr(chat_module, "ClientGuard", _fake_guard())
    turn = AsyncMock()
    monkeypatch.setattr(chat_module, "send_chat_message", turn)
    mock_ui = MagicMock()
    monkeypatch.setattr(chat_module, "ui", mock_ui)

    controller = chat_module._ChatTabController(_chat_context(job_actors, editor))  # type: ignore[arg-type]
    controller.chat_input = MagicMock()
    controller.chat_input.value = "Tell me about the findings"
    controller.send_btn = MagicMock()
    controller.chat_scroll = MagicMock()

    async with _session(test_engine) as db:
        await _set_share_level(db, job_actors["job_id"], editor["user_id"], "view")

    await controller._send_message()

    turn.assert_not_awaited()
    mock_ui.notify.assert_called()
    async with _session(test_engine) as db:
        count = await db.scalar(
            select(func.count())
            .select_from(JobChatMessage)
            .where(JobChatMessage.job_id == job_actors["job_id"])
        )
        assert count == 0


async def test_editor_with_edit_share_can_send_a_chat_turn(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = job_actors["editor"]
    _page_cookie(monkeypatch, editor["session_id"])
    monkeypatch.setattr(chat_module, "ClientGuard", _fake_guard())
    turn = AsyncMock(return_value="Here is what we found.")
    monkeypatch.setattr(chat_module, "send_chat_message", turn)
    mock_ui = MagicMock()
    monkeypatch.setattr(chat_module, "ui", mock_ui)

    controller = chat_module._ChatTabController(_chat_context(job_actors, editor))  # type: ignore[arg-type]
    controller.chat_input = MagicMock()
    controller.chat_input.value = "Tell me about the findings"
    controller.send_btn = MagicMock()
    controller.chat_scroll = MagicMock()

    await controller._send_message()

    turn.assert_awaited_once()
    _, job_id, message, _ = turn.await_args.args  # type: ignore[union-attr]
    assert str(job_id) == str(job_actors["job_id"])
    assert message == "Tell me about the findings"


async def test_demoted_editor_cannot_submit_feedback(
    test_engine: AsyncEngine,
    job_actors: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    editor = job_actors["editor"]
    _page_cookie(monkeypatch, editor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(feedback_module, "ui", mock_ui)

    async with _session(test_engine) as db:
        await _set_share_level(db, job_actors["job_id"], editor["user_id"], "view")

    job_dir = tmp_path / str(job_actors["job_id"])
    await feedback_module._submit_feedback_and_continue(
        job_dir,
        str(job_actors["job_id"]),
        str(editor["user_id"]),
        1,
        "Focus on metabolic pathways",
    )

    mock_ui.notify.assert_called()
    async with _session(test_engine) as db:
        row = await db.get(Job, job_actors["job_id"])
        assert row is not None
        assert row.status == JobStatus.AWAITING_FEEDBACK.value
        count = await db.scalar(
            select(func.count())
            .select_from(FeedbackHistory)
            .where(FeedbackHistory.job_id == job_actors["job_id"])
        )
        assert count == 0


async def test_editor_with_edit_share_can_submit_feedback(
    test_engine: AsyncEngine,
    job_actors: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    editor = job_actors["editor"]
    _page_cookie(monkeypatch, editor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(feedback_module, "ui", mock_ui)

    job_dir = tmp_path / str(job_actors["job_id"])
    await feedback_module._submit_feedback_and_continue(
        job_dir,
        str(job_actors["job_id"]),
        str(editor["user_id"]),
        1,
        "Focus on metabolic pathways",
    )

    async with _session(test_engine) as db:
        row = await db.get(Job, job_actors["job_id"])
        assert row is not None
        assert row.status == JobStatus.RUNNING.value
        feedback_rows = (
            (
                await db.execute(
                    select(FeedbackHistory).where(FeedbackHistory.job_id == job_actors["job_id"])
                )
            )
            .scalars()
            .all()
        )
        assert [f.feedback_text for f in feedback_rows] == ["Focus on metabolic pathways"]


def _poll_context(
    job_actors: dict, viewer: dict, job_dir: Path, baseline: job_detail_common._PageSnapshot
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=str(job_actors["job_id"]),
        user_id=str(viewer["user_id"]),
        job_dir=job_dir,
        job_info=None,
        job_manager=None,
        ks_data=None,
        action_counts={},
        state={
            "status": JobStatus.AWAITING_FEEDBACK,
            "stats_model": None,
            "snapshot": baseline,
        },
    )


async def test_viewer_whose_share_is_removed_stops_receiving_status_updates(
    test_engine: AsyncEngine,
    job_actors: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    viewer = job_actors["viewer"]
    _page_cookie(monkeypatch, viewer["session_id"])
    job_dir = tmp_path / str(job_actors["job_id"])
    probe = await job_detail_common._page_snapshot(
        str(job_actors["job_id"]), str(viewer["user_id"]), job_dir
    )
    assert probe is not None, "the viewer's share must still make the job visible"
    context = _poll_context(job_actors, viewer, job_dir, probe[1])
    timer = MagicMock()
    holder: dict[str, object] = {"timer": timer}
    render_job_stats = MagicMock()
    timeline = iterations_module._Timeline(context)  # type: ignore[arg-type]

    await timeline_module._check_and_refresh(context, render_job_stats, timeline, holder)  # type: ignore[arg-type]
    timer.deactivate.assert_not_called()

    async with _session(test_engine) as db:
        await db.execute(
            delete(JobShare).where(
                JobShare.job_id == job_actors["job_id"],
                JobShare.shared_with_user_id == viewer["user_id"],
            )
        )
        await db.commit()

    mock_ui = MagicMock()
    monkeypatch.setattr(timeline_module, "ui", mock_ui)
    monkeypatch.setattr(timeline_module, "is_client_connected", lambda: True)
    common_ui = MagicMock()
    monkeypatch.setattr(job_detail_common, "ui", common_ui)

    await timeline_module._check_and_refresh(context, render_job_stats, timeline, holder)  # type: ignore[arg-type]

    timer.deactivate.assert_called_once()
    mock_ui.context.client.content.clear.assert_called_once()
    render_job_stats.refresh.assert_not_called()
    common_ui.label.assert_called()


def _share_controller(job_actors: dict, target_email: str) -> dialogs_module._ShareDialogController:
    controller = dialogs_module._ShareDialogController(
        job_id=str(job_actors["job_id"]),
        shares_container=MagicMock(),
        public_switch=MagicMock(),
        email_input=MagicMock(),
        selected_user_container=MagicMock(),
        share_action_row=MagicMock(),
        permission_select=SimpleNamespace(value="view"),  # type: ignore[arg-type]
    )
    controller.selected_user_state = {"email": target_email, "name": target_email}
    return controller


async def test_owner_with_deleted_session_cannot_share(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = job_actors["owner"]
    target = job_actors["target"]
    _page_cookie(monkeypatch, owner["session_id"])
    monkeypatch.setattr(dialogs_module, "get_current_user_id", lambda: str(owner["user_id"]))
    mock_ui = MagicMock()
    monkeypatch.setattr(dialogs_module, "ui", mock_ui)

    async with _session(test_engine) as db:
        await _delete_session(db, owner["session_id"])

    controller = _share_controller(job_actors, target.email)
    await controller.do_share()

    mock_ui.notify.assert_called()
    async with _session(test_engine) as db:
        count = await db.scalar(
            select(func.count())
            .select_from(JobShare)
            .where(
                JobShare.job_id == job_actors["job_id"],
                JobShare.shared_with_user_id == target.id,
            )
        )
        assert count == 0


async def test_editor_with_edit_share_cannot_share(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = job_actors["editor"]
    target = job_actors["target"]
    _page_cookie(monkeypatch, editor["session_id"])
    monkeypatch.setattr(dialogs_module, "get_current_user_id", lambda: str(editor["user_id"]))
    mock_ui = MagicMock()
    monkeypatch.setattr(dialogs_module, "ui", mock_ui)

    controller = _share_controller(job_actors, target.email)
    await controller.do_share()

    mock_ui.notify.assert_called()
    async with _session(test_engine) as db:
        count = await db.scalar(
            select(func.count())
            .select_from(JobShare)
            .where(
                JobShare.job_id == job_actors["job_id"],
                JobShare.shared_with_user_id == target.id,
            )
        )
        assert count == 0


async def test_owner_with_deleted_session_cannot_change_visibility(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = job_actors["owner"]
    _page_cookie(monkeypatch, owner["session_id"])
    monkeypatch.setattr(dialogs_module, "get_current_user_id", lambda: str(owner["user_id"]))
    mock_ui = MagicMock()
    monkeypatch.setattr(dialogs_module, "ui", mock_ui)

    async with _session(test_engine) as db:
        await _delete_session(db, owner["session_id"])

    controller = dialogs_module._ShareDialogController(
        job_id=str(job_actors["job_id"]),
        shares_container=MagicMock(),
        public_switch=MagicMock(),
        email_input=MagicMock(),
        selected_user_container=MagicMock(),
        share_action_row=MagicMock(),
        permission_select=SimpleNamespace(value="view"),  # type: ignore[arg-type]
    )
    await controller.on_public_change(SimpleNamespace(value=True))

    mock_ui.notify.assert_called()
    async with _session(test_engine) as db:
        row = await db.get(Job, job_actors["job_id"])
        assert row is not None
        assert row.is_public is False


async def test_live_owner_can_change_visibility(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = job_actors["owner"]
    _page_cookie(monkeypatch, owner["session_id"])
    monkeypatch.setattr(dialogs_module, "get_current_user_id", lambda: str(owner["user_id"]))
    mock_ui = MagicMock()
    monkeypatch.setattr(dialogs_module, "ui", mock_ui)

    controller = dialogs_module._ShareDialogController(
        job_id=str(job_actors["job_id"]),
        shares_container=MagicMock(),
        public_switch=MagicMock(),
        email_input=MagicMock(),
        selected_user_container=MagicMock(),
        share_action_row=MagicMock(),
        permission_select=SimpleNamespace(value="view"),  # type: ignore[arg-type]
    )
    await controller.on_public_change(SimpleNamespace(value=True))

    async with _session(test_engine) as db:
        row = await db.get(Job, job_actors["job_id"])
        assert row is not None
        assert row.is_public is True


def _job_manager_mock() -> MagicMock:
    job_manager = MagicMock()
    job_manager.ACTIVE_STATUSES = {
        JobStatus.PENDING,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.GENERATING_REPORT,
    }
    job_manager.get_job = AsyncMock(return_value=SimpleNamespace(status=JobStatus.RUNNING))
    job_manager.cancel_job = AsyncMock()
    job_manager.delete_job = AsyncMock()
    return job_manager


async def test_owner_with_deleted_session_cannot_delete_or_cancel(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = job_actors["owner"]
    _page_cookie(monkeypatch, owner["session_id"])
    monkeypatch.setattr(dialogs_module, "get_current_user_id", lambda: str(owner["user_id"]))
    mock_ui = MagicMock()
    monkeypatch.setattr(dialogs_module, "ui", mock_ui)

    async with _session(test_engine) as db:
        await _delete_session(db, owner["session_id"])

    job_manager = _job_manager_mock()
    await dialogs_module.confirm_delete_job(
        str(job_actors["job_id"]), job_manager, None, MagicMock()
    )

    job_manager.cancel_job.assert_not_awaited()
    job_manager.delete_job.assert_not_awaited()
    mock_ui.notify.assert_called()
    async with _session(test_engine) as db:
        assert await db.get(Job, job_actors["job_id"]) is not None


async def test_live_owner_can_delete_and_cancel(
    test_engine: AsyncEngine, job_actors: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = job_actors["owner"]
    _page_cookie(monkeypatch, owner["session_id"])
    monkeypatch.setattr(dialogs_module, "get_current_user_id", lambda: str(owner["user_id"]))
    mock_ui = MagicMock()
    monkeypatch.setattr(dialogs_module, "ui", mock_ui)

    job_manager = _job_manager_mock()
    await dialogs_module.confirm_delete_job(
        str(job_actors["job_id"]), job_manager, None, MagicMock()
    )

    job_manager.cancel_job.assert_awaited_once()
    job_manager.delete_job.assert_awaited_once()


async def test_demoted_admin_cannot_regenerate_report(
    test_engine: AsyncEngine, admin_actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(report_module, "ui", mock_ui)

    async with _session(test_engine) as db:
        await _demote_admin(db, admin_actor["user_id"])

    context = SimpleNamespace(
        job_id=str(uuid4()),
        job_manager=SimpleNamespace(regenerate_report=AsyncMock()),
    )
    await report_module._regenerate_report(context)  # type: ignore[arg-type]

    context.job_manager.regenerate_report.assert_not_awaited()  # type: ignore[attr-defined]
    mock_ui.notify.assert_called()


async def test_live_admin_can_regenerate_report(
    test_engine: AsyncEngine, admin_actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    _page_cookie(monkeypatch, admin_actor["session_id"])
    mock_ui = MagicMock()
    monkeypatch.setattr(report_module, "ui", mock_ui)

    context = SimpleNamespace(
        job_id=str(uuid4()),
        job_manager=SimpleNamespace(regenerate_report=AsyncMock()),
    )
    await report_module._regenerate_report(context)  # type: ignore[arg-type]

    context.job_manager.regenerate_report.assert_awaited_once()  # type: ignore[attr-defined]

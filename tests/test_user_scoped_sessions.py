"""Shared code the web runs for a user reaches only that user's jobs."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database import get_session_ctx
from openscientist.database.models import Job, User
from openscientist.database.models.job_share import JobShare
from openscientist.database.rls import set_current_user
from openscientist.database.session import thread_safe_user_session
from openscientist.job_chat import load_job_context
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import update_job_status
from openscientist.webapp_components.pages import job_detail
from openscientist.webapp_components.pages.job_detail import (
    chat,
    common,
    feedback,
    iterations,
    prompt,
    report,
    timeline,
)
from openscientist.webapp_components.utils.permissions import JobAccess


@dataclass(frozen=True)
class _Fixture:
    job_id: str
    owner: UUID
    stranger: UUID


@pytest_asyncio.fixture
async def alices_job(test_engine: AsyncEngine) -> AsyncGenerator[_Fixture, None]:
    """Alice's job with one finding, and Bob, who has no share in it."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        alice = User(email=f"alice-{uuid4().hex}@scoped.test", name="Alice")
        bob = User(email=f"bob-{uuid4().hex}@scoped.test", name="Bob")
        session.add_all([alice, bob])
        await session.flush()
        job = Job(
            owner_id=alice.id, research_question="Alice's question", status="awaiting_feedback"
        )
        session.add(job)
        await session.commit()
    ks = KnowledgeState(str(job.id), "Alice's question", 3)
    ks.add_finding("Alice's finding", "evidence")
    await ks.save_to_database(str(job.id))
    yield _Fixture(job_id=str(job.id), owner=alice.id, stranger=bob.id)
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job.id})
        await conn.execute(
            text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": [alice.id, bob.id]}
        )


async def _count(test_engine: AsyncEngine, sql: str, job_id: str) -> int:
    async with test_engine.connect() as conn:
        return int((await conn.execute(text(sql), {"id": job_id})).scalar_one())


async def _status(test_engine: AsyncEngine, job_id: str) -> str:
    async with test_engine.connect() as conn:
        result = await conn.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id})
        return str(result.scalar_one())


async def test_a_user_loads_the_knowledge_of_their_own_job_only(alices_job: _Fixture) -> None:
    async with thread_safe_user_session(alices_job.owner) as session:
        mine = await KnowledgeState.load_from_database(alices_job.job_id, session)
    assert [f["title"] for f in mine.data["findings"]] == ["Alice's finding"]
    with pytest.raises(ValueError, match="not found"):
        async with thread_safe_user_session(alices_job.stranger) as session:
            await KnowledgeState.load_from_database(alices_job.job_id, session)


@pytest.mark.asyncio
async def test_a_user_cannot_save_knowledge_into_someone_elses_job(
    alices_job: _Fixture, test_engine: AsyncEngine
) -> None:
    ks = await KnowledgeState.load_from_database(alices_job.job_id)
    ks.add_finding("Planted finding", "evidence")
    with pytest.raises(DBAPIError):
        async with thread_safe_user_session(alices_job.stranger) as session:
            await ks.save_to_database(alices_job.job_id, session)
    planted = "SELECT count(*) FROM findings WHERE job_id = :id AND text = 'Planted finding'"
    assert await _count(test_engine, planted, alices_job.job_id) == 0


@pytest.mark.asyncio
async def test_continuing_after_feedback_changes_only_the_users_own_job(
    alices_job: _Fixture, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    job_dir = tmp_path / alices_job.job_id
    await update_job_status(
        job_dir,
        "running",
        allowed_from=("awaiting_feedback",),
        open_session=partial(thread_safe_user_session, alices_job.stranger),
    )
    assert await _status(test_engine, alices_job.job_id) == "awaiting_feedback"
    await update_job_status(
        job_dir,
        "running",
        allowed_from=("awaiting_feedback",),
        open_session=partial(thread_safe_user_session, alices_job.owner),
    )
    assert await _status(test_engine, alices_job.job_id) == "running"


@pytest.mark.asyncio
async def test_chat_context_comes_only_from_the_chatting_users_jobs(
    alices_job: _Fixture,
) -> None:
    async with get_session_ctx() as session:
        await set_current_user(session, alices_job.owner)
        own = await load_job_context(alices_job.job_id, session)
    async with get_session_ctx() as session:
        await set_current_user(session, alices_job.stranger)
        foreign = await load_job_context(alices_job.job_id, session)
    assert "Alice's question" in own
    assert "Alice's finding" in own
    assert "Alice" not in foreign


async def _feedback(job_id: str) -> list[str]:
    ks = await KnowledgeState.load_from_database(job_id)
    return [entry["text"] for entry in ks.data.get("feedback_history", [])]


@pytest.mark.asyncio
async def test_the_owners_feedback_is_saved_and_continues_the_job(
    alices_job: _Fixture, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    with (
        patch.object(feedback, "ui"),
        patch.object(
            feedback,
            "current_job_access",
            AsyncMock(return_value=JobAccess(is_owner=True, can_edit=True)),
        ),
    ):
        await feedback._submit_feedback_and_continue(
            tmp_path / alices_job.job_id, alices_job.job_id, str(alices_job.owner), 1, "  Recheck  "
        )
    assert await _feedback(alices_job.job_id) == ["Recheck"]
    assert await _status(test_engine, alices_job.job_id) == "running"


@pytest.mark.asyncio
async def test_a_strangers_feedback_changes_nothing(
    alices_job: _Fixture, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    with (
        patch.object(feedback, "ui"),
        patch.object(
            feedback,
            "current_job_access",
            AsyncMock(return_value=JobAccess(is_owner=True, can_edit=True)),
        ),
        pytest.raises(ValueError, match="not found"),
    ):
        await feedback._submit_feedback_and_continue(
            tmp_path / alices_job.job_id, alices_job.job_id, str(alices_job.stranger), 1, "Planted"
        )
    assert await _feedback(alices_job.job_id) == []
    assert await _status(test_engine, alices_job.job_id) == "awaiting_feedback"


@pytest.mark.asyncio
async def test_the_job_page_shows_the_knowledge_state_to_its_owner_only(
    alices_job: _Fixture,
) -> None:
    data, _, error = await common._load_knowledge_state(alices_job.job_id, str(alices_job.owner))
    assert error is None
    assert data is not None
    assert [f["title"] for f in data["findings"]] == ["Alice's finding"]
    data, _, error = await common._load_knowledge_state(alices_job.job_id, str(alices_job.stranger))
    assert data is None
    assert error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("share", "rights"),
    [(None, None), ("view", (False, False, True)), ("edit", (False, True, True))],
)
async def test_a_users_rights_on_the_job_page_follow_their_share(
    alices_job: _Fixture, test_engine: AsyncEngine, share: str | None, rights: object
) -> None:
    if share is not None:
        async with AsyncSession(test_engine) as session:
            session.add(
                JobShare(
                    job_id=UUID(alices_job.job_id),
                    shared_with_user_id=alices_job.stranger,
                    permission_level=share,
                )
            )
            await session.commit()
    bob = str(alices_job.stranger)
    job = await job_detail._load_db_job_for_user(alices_job.job_id, bob)
    if rights is None:
        assert job is None
    else:
        assert await job_detail._resolve_job_permissions(alices_job.job_id, bob, job) == rights
    own = await job_detail._load_db_job_for_user(alices_job.job_id, str(alices_job.owner))
    assert await job_detail._resolve_job_permissions(
        alices_job.job_id, str(alices_job.owner), own
    ) == (True, True, True)


_PAGE_UI_KINDS = (
    "card",
    "column",
    "row",
    "element",
    "expansion",
    "scroll_area",
    "tabs",
    "tab",
    "tab_panels",
    "tab_panel",
    "dialog",
    "textarea",
    "button",
)


def _page_ui_mock() -> tuple[MagicMock, dict[str, list[MagicMock]]]:
    """A NiceGUI stand-in: container elements are context managers, and every
    element is recorded per kind so tests can reach widget state."""
    created: dict[str, list[MagicMock]] = {}

    def make(kind: str):
        def factory(*_args: object, **_kwargs: object) -> MagicMock:
            element = MagicMock()
            element.__enter__ = MagicMock(return_value=element)
            element.__exit__ = MagicMock(return_value=False)
            element.classes.return_value = element
            created.setdefault(kind, []).append(element)
            return element

        return MagicMock(side_effect=factory)

    mock = MagicMock()
    for kind in _PAGE_UI_KINDS:
        setattr(mock, kind, make(kind))
    return mock, created


@pytest.mark.asyncio
async def test_the_job_page_follows_the_job_from_feedback_to_completion(
    alices_job: _Fixture, test_engine: AsyncEngine, tmp_path: Path
) -> None:
    from openscientist.auth import middleware
    from openscientist.job.types import JobInfo, JobStatus
    from openscientist.job_manager import JobManager
    from openscientist.webapp_components.utils import client_guard

    job_id = alices_job.job_id
    mock_ui, created = _page_ui_mock()
    mock_ui.context.client.page.response_timeout = 30.0
    job_info = JobInfo(
        job_id=job_id,
        research_question="Alice's question",
        status=JobStatus.AWAITING_FEEDBACK,
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-02T00:00:00+00:00",
    )
    manager = SimpleNamespace(
        jobs_dir=tmp_path,
        get_job=AsyncMock(return_value=job_info),
        cancel_job=AsyncMock(),
        delete_job=AsyncMock(),
        ACTIVE_STATUSES=JobManager.ACTIVE_STATUSES,
    )
    storage: dict[str, object] = {
        "user_id": str(alices_job.owner),
        "email": "alice@scoped.test",
        "name": "Alice",
        "is_admin": False,
        "is_approved": True,
        "can_start_jobs": True,
        "authenticated": True,
    }
    fake_app = SimpleNamespace(storage=SimpleNamespace(user=storage))

    async def _validated(_token: str) -> dict[str, object]:
        return {
            "user_id": str(alices_job.owner),
            "email": "alice@scoped.test",
            "name": "Alice",
            "is_admin": False,
            "is_approved": True,
            "can_start_jobs": True,
        }

    with ExitStack() as stack:
        for patcher in (
            patch("openscientist.web_app.get_job_manager", return_value=manager),
            patch.object(job_detail, "ui", mock_ui),
            patch.object(chat, "ui", mock_ui),
            patch.object(feedback, "ui", mock_ui),
            patch.object(iterations, "ui", mock_ui),
            patch.object(prompt, "ui", mock_ui),
            patch.object(report, "ui", mock_ui),
            patch.object(timeline, "ui", mock_ui),
            patch("openscientist.webapp_components.components.badges.ui", mock_ui),
            patch("openscientist.webapp_components.components.banners.ui", mock_ui),
            patch("openscientist.webapp_components.components.common.ui", mock_ui),
            patch("openscientist.webapp_components.components.dialogs.ui", mock_ui),
            patch("openscientist.webapp_components.components.job_table.ui", mock_ui),
            patch("openscientist.webapp_components.components.navigation.ui", mock_ui),
            patch("openscientist.webapp_components.components.navigation.app", fake_app),
            patch("openscientist.webapp_components.components.notifications.ui", mock_ui),
            patch.object(client_guard, "is_client_connected", return_value=True),
            patch.object(client_guard, "ui", mock_ui),
            patch.object(middleware, "app", fake_app),
            patch.object(middleware, "get_session_token", return_value="test-token"),
            patch.object(middleware, "ui", mock_ui),
            patch.object(middleware, "validate_session", _validated),
            patch("openscientist.webapp_components.utils.permissions.validate_session", _validated),
        ):
            stack.enter_context(patcher)
        # Alice opens her job's page: the page renders with the feedback panel.
        await job_detail.job_detail_page(job_id)
        labels = [c.args[0] for c in mock_ui.label.call_args_list if c.args]
        assert "Iteration 1 Complete - Awaiting Your Input" in labels
        assert mock_ui.page_title.called

        # Alice types feedback and clicks "Submit & Continue": the feedback is
        # persisted and the job resumes running.
        created["textarea"][0].value = "Recheck the evidence"
        submit = [
            c for c in mock_ui.button.call_args_list if c.args and c.args[0] == "Submit & Continue"
        ][0].kwargs["on_click"]
        await submit()
        assert await _feedback(job_id) == ["Recheck the evidence"]
        assert await _status(test_engine, job_id) == "running"

        # The poll timer follows the job: once it completes, the page reloads.
        poll = mock_ui.timer.call_args_list[0].args[1]
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET status = 'completed' WHERE id = :id"), {"id": job_id}
            )
        await poll()
        mock_ui.navigate.to.assert_called_with(f"/job/{job_id}")
        mock_ui.timer.return_value.deactivate.assert_called()

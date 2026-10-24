"""Tests for the share dialog's public-visibility switch (issue #296)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, User
from openscientist.webapp_components import ui_components
from tests.helpers import fake_admin_session


async def _job(db_session: AsyncSession, owner: User, *, is_public: bool = False) -> Job:
    job = Job(
        owner_id=owner.id,
        research_question="Share dialog visibility job",
        status="completed",
        is_public=is_public,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


async def _stored_is_public(db_session: AsyncSession, job: Job) -> bool:
    result = await db_session.execute(select(Job.is_public).where(Job.id == job.id))
    return bool(result.scalar_one())


@pytest.mark.asyncio
async def test_owner_can_make_a_job_public_from_the_dialog(
    db_session: AsyncSession, test_user: User
) -> None:
    job = await _job(db_session, test_user)

    with patch.object(ui_components, "get_admin_session", fake_admin_session(db_session)):
        assert await ui_components._load_job_is_public(str(job.id)) is False
        made_public = await ui_components._set_job_public_from_ui(
            str(job.id), True, str(test_user.id)
        )
        assert await ui_components._load_job_is_public(str(job.id)) is True
        made_private = await ui_components._set_job_public_from_ui(
            str(job.id), False, str(test_user.id)
        )

    assert made_public == (True, "Any signed-in user with the link can now view this job")
    assert made_private == (True, "This job is private again")
    assert await _stored_is_public(db_session, job) is False


@pytest.mark.asyncio
async def test_non_owner_cannot_change_visibility_from_the_dialog(
    db_session: AsyncSession, test_user: User, test_user2: User
) -> None:
    job = await _job(db_session, test_user)

    with patch.object(ui_components, "get_admin_session", fake_admin_session(db_session)):
        result = await ui_components._set_job_public_from_ui(str(job.id), True, str(test_user2.id))

    assert result == (False, "Only the job owner can change its visibility")
    assert await _stored_is_public(db_session, job) is False


def _controller() -> ui_components._ShareDialogController:
    return ui_components._ShareDialogController(
        job_id="job-1",
        shares_container=MagicMock(),
        search_input=MagicMock(),
        search_results=MagicMock(),
        selected_user_container=MagicMock(),
        share_action_row=MagicMock(),
        permission_select=MagicMock(),
        public_switch=SimpleNamespace(value=False),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_switch_shows_stored_visibility_without_saving_it() -> None:
    controller = _controller()
    save = AsyncMock()

    with (
        patch.object(ui_components, "_load_job_is_public", AsyncMock(return_value=True)),
        patch.object(ui_components, "_set_job_public_from_ui", save),
    ):
        await controller.refresh_public_switch()
        # A value-change event fired while syncing must not write back.
        controller._syncing_public_switch = True
        await controller.on_public_change(SimpleNamespace(value=True))

    assert controller.public_switch.value is True
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_change_saves_and_reports_result() -> None:
    controller = _controller()
    save = AsyncMock(return_value=(True, "This job is private again"))

    with (
        patch.object(ui_components, "get_current_user_id", return_value="owner-1"),
        patch.object(ui_components, "_set_job_public_from_ui", save),
        patch.object(ui_components, "_load_job_is_public", AsyncMock(return_value=False)),
        patch.object(ui_components, "ui") as mock_ui,
    ):
        await controller.on_public_change(SimpleNamespace(value=False))

    save.assert_awaited_once_with("job-1", False, "owner-1")
    mock_ui.notify.assert_called_once_with("This job is private again", type="positive")
    assert controller.public_switch.value is False


@pytest.mark.asyncio
async def test_switch_change_requires_a_signed_in_user() -> None:
    controller = _controller()
    save = AsyncMock()

    with (
        patch.object(ui_components, "get_current_user_id", return_value=None),
        patch.object(ui_components, "_set_job_public_from_ui", save),
        patch.object(ui_components, "_load_job_is_public", AsyncMock(return_value=False)),
        patch.object(ui_components, "ui") as mock_ui,
    ):
        await controller.on_public_change(SimpleNamespace(value=True))

    save.assert_not_awaited()
    mock_ui.notify.assert_called_once_with(
        "You must be signed in to change visibility", type="negative"
    )


@pytest.mark.asyncio
async def test_switch_change_reports_an_unexpected_error() -> None:
    controller = _controller()

    with (
        patch.object(ui_components, "get_current_user_id", return_value="owner-1"),
        patch.object(ui_components, "_set_job_public_from_ui", AsyncMock(side_effect=RuntimeError)),
        patch.object(ui_components, "_load_job_is_public", AsyncMock(return_value=False)),
        patch.object(ui_components, "ui") as mock_ui,
    ):
        await controller.on_public_change(SimpleNamespace(value=True))

    mock_ui.notify.assert_called_once_with("Error changing visibility", type="negative")
    assert controller.public_switch.value is False


@pytest.mark.asyncio
async def test_refresh_shares_also_refreshes_the_switch() -> None:
    controller = _controller()

    with (
        patch.object(ui_components, "_load_job_is_public", AsyncMock(return_value=True)),
        patch.object(ui_components, "_load_job_shares", AsyncMock(return_value=[])),
        patch.object(ui_components, "_render_share_rows"),
    ):
        await controller.refresh_shares()

    assert controller.public_switch.value is True


@pytest.mark.asyncio
async def test_switch_keeps_its_value_when_visibility_cannot_be_loaded() -> None:
    controller = _controller()

    with patch.object(ui_components, "_load_job_is_public", AsyncMock(side_effect=RuntimeError)):
        await controller.refresh_public_switch()

    assert controller.public_switch.value is False


def test_share_dialog_wires_the_public_switch() -> None:
    with (
        patch.object(ui_components, "ui") as mock_ui,
        patch.object(ui_components, "render_job_id_badge"),
    ):
        ui_components.render_share_dialog("job-1")

    mock_ui.switch.assert_called_once_with("Anyone signed in can view this job")
    switch = mock_ui.switch.return_value
    switch.on_value_change.assert_called_once()
    handler = switch.on_value_change.call_args.args[0]
    assert handler.__func__ is ui_components._ShareDialogController.on_public_change

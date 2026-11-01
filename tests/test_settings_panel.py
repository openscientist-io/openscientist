"""The admin Settings tab's save path, its guards, and the form's display."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import AsyncIterator, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.selectable import Select

from openscientist.admin_settings import AdminSettingsValues
from openscientist.database.models import AdminSettingChange, AdminSettings, User
from openscientist.webapp_components.pages import settings_panel
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.helpers import fake_admin_session

_BASE = {
    "max_job_cost_usd": 12.5,
    "max_project_spend_total_usd": None,
    "max_project_spend_24h_usd": 500.0,
    "warn_project_spend_24h_usd": 100.0,
    "agent_cpu": 2.0,
    "executor_cpu": 0.5,
    "agent_memory_mib": 8192,
    "executor_memory_mib": 512,
    "agent_timeout_seconds": 14400,
    "executor_timeout_seconds": 120,
    "max_file_size_mb": 1000,
    "max_concurrent_jobs": 0,
    "scheduler_window_hours": 24.0,
    "session_duration_days": 30,
    "airgapped": False,
}

_UPDATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def _default_row_after(test_engine: AsyncEngine) -> AsyncIterator[None]:
    """Every test leaves the default row the suite expects."""
    yield
    await reset_admin_settings_row(test_engine)


async def _write_row(db_session: AsyncSession, **overrides: object) -> None:
    await db_session.execute(delete(AdminSettings))
    db_session.add(AdminSettings(id=True, **{**_BASE, **overrides}))
    await db_session.commit()


async def _commit_row(test_engine: AsyncEngine) -> None:
    await set_admin_settings_row(test_engine, **_BASE)


async def _commit_user(test_engine: AsyncEngine) -> UUID:
    async with test_engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO users (email, name) VALUES (:email, 'Admin') RETURNING id"),
            {"email": f"settings-{uuid4().hex}@example.com"},
        )
        user_id: UUID = result.scalar_one()
        return user_id


def _values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = dict(_BASE)
    values.update(overrides)
    return values


def _floor(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        settings_panel,
        "get_settings",
        lambda: SimpleNamespace(airgap=SimpleNamespace(enabled=enabled)),
    )


def _admin(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession, user: User) -> None:
    monkeypatch.setattr(settings_panel, "is_current_user_admin", lambda: True)
    monkeypatch.setattr(settings_panel, "get_current_user_id", lambda: str(user.id))
    monkeypatch.setattr(settings_panel, "get_admin_session", fake_admin_session(db_session))


async def _change_pairs(db_session: AsyncSession) -> list[tuple[str, str | None, str | None]]:
    changes = (
        (
            await db_session.execute(
                select(AdminSettingChange).order_by(
                    AdminSettingChange.changed_at, AdminSettingChange.id
                )
            )
        )
        .scalars()
        .all()
    )
    return [(c.setting, c.old_value, c.new_value) for c in changes]


async def _row(db_session: AsyncSession) -> AdminSettings:
    return (await db_session.execute(select(AdminSettings))).scalar_one()


@pytest.mark.asyncio
async def test_a_save_records_only_the_changed_settings(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="admin@example.com", name="Admin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    await _write_row(db_session)
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, False)

    success, _ = await settings_panel.save_admin_settings(
        _values(agent_cpu=4.0, max_project_spend_24h_usd=None, airgapped=True)
    )

    assert success is True
    row = await _row(db_session)
    assert row.agent_cpu == 4.0
    assert row.max_project_spend_24h_usd is None
    assert row.airgapped is True
    assert row.updated_by == user.id
    assert row.updated_at is not None

    assert sorted(await _change_pairs(db_session)) == [
        ("Agent CPU", "2.0", "4.0"),
        ("Air-gap", "off", "on"),
        ("Project spend (24 h)", "500.0", "no limit"),
    ]
    changes = (await db_session.execute(select(AdminSettingChange))).scalars().all()
    assert all(c.changed_by == user.id for c in changes)


@pytest.mark.asyncio
async def test_an_unchanged_save_writes_no_history(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="admin@example.com", name="Admin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    await _write_row(db_session)
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, False)

    success, _ = await settings_panel.save_admin_settings(_values())

    assert success is True
    row = await _row(db_session)
    assert row.updated_by is None
    assert await _change_pairs(db_session) == []


@pytest.mark.asyncio
async def test_an_out_of_bounds_value_is_refused_and_writes_nothing(
    db_session: AsyncSession,
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The row and user are committed on their own, because a CHECK violation
    # aborts the db_session's transaction and rolls its rows back with it.
    user_id = await _commit_user(test_engine)
    await _commit_row(test_engine)
    user = User(email="admin@example.com", name="Admin", id=user_id)
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, False)
    success, message = await settings_panel.save_admin_settings(_values(agent_cpu=0.0))

    assert success is False
    assert "Agent CPU" in message
    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.agent_cpu == 2.0
    assert row.updated_by is None
    assert await _change_pairs(db_session) == []


@pytest.mark.asyncio
async def test_the_airgap_switch_cannot_be_turned_off_while_the_floor_locks_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="admin@example.com", name="Admin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    await _write_row(db_session, airgapped=True)
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, True)

    success, _ = await settings_panel.save_admin_settings(_values(airgapped=False))

    assert success is False
    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.airgapped is True
    assert row.updated_by is None
    assert await _change_pairs(db_session) == []


@pytest.mark.asyncio
async def test_a_non_admin_cannot_save(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="admin@example.com", name="Admin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    await _write_row(db_session)
    monkeypatch.setattr(settings_panel, "is_current_user_admin", lambda: False)
    monkeypatch.setattr(settings_panel, "get_admin_session", fake_admin_session(db_session))
    _floor(monkeypatch, False)

    success, _ = await settings_panel.save_admin_settings(_values(agent_cpu=4.0))

    assert success is False
    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.agent_cpu == 2.0
    assert row.updated_by is None
    assert await _change_pairs(db_session) == []


class _HistoryResult:
    def all(self) -> list[tuple[AdminSettingChange, User | None]]:
        return []


class _HistorySession:
    async def execute(self, stmt: Select) -> _HistoryResult:
        return _HistoryResult()


@asynccontextmanager
async def _history_session() -> AsyncIterator[_HistorySession]:
    yield _HistorySession()


def _make_element() -> MagicMock:
    """One NiceGUI stand-in element that also serves as a context manager."""
    element = MagicMock()
    element.__enter__ = MagicMock(return_value=element)
    element.__exit__ = MagicMock(return_value=False)
    element.classes.return_value = element
    return element


def _panel_ui_mock() -> MagicMock:
    """A NiceGUI stand-in whose container elements serve as context managers."""
    mock = MagicMock()
    for name in ("card", "column", "row", "element"):
        setattr(mock, name, _make_element)
    return mock


def _panel_values(airgap_locked: bool) -> AdminSettingsValues:
    return AdminSettingsValues(
        max_job_cost_usd=12.5,
        max_project_spend_total_usd=None,
        max_project_spend_24h_usd=500.0,
        warn_project_spend_24h_usd=100.0,
        agent_cpu=2.0,
        executor_cpu=0.5,
        agent_memory_mib=8192,
        executor_memory_mib=512,
        agent_timeout_seconds=14400,
        executor_timeout_seconds=120,
        max_file_size_mb=1000,
        max_concurrent_jobs=0,
        scheduler_window_hours=24.0,
        airgapped=True,
        airgap_locked=airgap_locked,
        session_duration_days=30,
        updated_at=_UPDATED_AT,
        updated_by=None,
    )


def test_every_setting_column_has_a_form_field() -> None:
    """A column added to the settings row cannot be left out of the form."""
    columns = {column.name for column in AdminSettings.__table__.columns} - {
        "id",
        "updated_at",
        "updated_by",
    }
    form = {column for column, *_ in settings_panel._FIELDS} | {settings_panel._AIRGAP[0]}
    assert form == columns


@pytest.mark.asyncio
@pytest.mark.parametrize("airgap_locked", [False, True])
async def test_the_airgap_switch_is_disabled_exactly_when_the_floor_locks_it(
    monkeypatch: pytest.MonkeyPatch, airgap_locked: bool
) -> None:
    monkeypatch.setattr(
        settings_panel,
        "current_admin_settings",
        AsyncMock(return_value=_panel_values(airgap_locked)),
    )
    monkeypatch.setattr(settings_panel, "get_admin_session", _history_session())
    mock_ui = _panel_ui_mock()
    with (
        patch.object(settings_panel, "ui", mock_ui),
        patch.object(settings_panel, "render_empty_state", MagicMock()),
        patch.object(settings_panel, "render_alert_banner", MagicMock()),
    ):
        await settings_panel.render_settings_panel()

    switch = mock_ui.switch
    assert switch.call_args.kwargs["value"] is True
    if airgap_locked:
        switch.return_value.props.assert_called_with("disable")
    else:
        switch.return_value.props.assert_not_called()


def test_a_history_row_names_the_changer_or_unknown() -> None:
    """The history shows the changer's email, the name if it has none, or Unknown."""
    assert settings_panel._who(None) == "Unknown"
    assert (
        settings_panel._who(User(email="author@example.com", name="Author")) == "author@example.com"
    )
    assert settings_panel._who(User(email="", name="JustName")) == "JustName"
    assert settings_panel._who(User(email="", name="")) == "Unknown"


def test_current_user_uuid_is_none_outside_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a signed-in visitor the change would carry no author."""
    monkeypatch.setattr(settings_panel, "get_current_user_id", lambda: None)
    assert settings_panel._current_user_uuid() is None


def test_current_user_uuid_reads_the_signed_in_visitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = str(uuid4())
    monkeypatch.setattr(settings_panel, "get_current_user_id", lambda: user_id)
    assert settings_panel._current_user_uuid() == UUID(user_id)


def test_a_rejection_without_a_named_check_names_a_setting() -> None:
    """A database rejection that names no check constraint still names a setting."""
    exc = IntegrityError("UPDATE admin_settings", {}, Exception("database error"))
    assert settings_panel._rejected_setting(exc) == "A setting"


@pytest.mark.asyncio
async def test_a_save_reports_that_the_row_is_missing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="admin@example.com", name="Admin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.execute(delete(AdminSettings))
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, False)

    success, _ = await settings_panel.save_admin_settings(_values(agent_cpu=4.0))

    assert success is False
    assert await _change_pairs(db_session) == []


def _collect_form() -> tuple[dict[str, ui.number], dict[str, ui.checkbox], ui.switch]:
    """A filled-in form, every budget ticked No limit and the switch on. The
    widgets are fakes carrying just the value the form reads."""
    numbers: dict[str, ui.number] = {}
    no_limit: dict[str, ui.checkbox] = {}
    for column, *_rest, kind in settings_panel._FIELDS:
        if kind == "budget":
            numbers[column] = cast(ui.number, SimpleNamespace(value=None))
            no_limit[column] = cast(ui.checkbox, SimpleNamespace(value=True))
        else:
            numbers[column] = cast(ui.number, SimpleNamespace(value=3))
    return numbers, no_limit, cast(ui.switch, SimpleNamespace(value=True))


def test_the_form_collects_each_column_with_its_type() -> None:
    """The form hands the row a typed value per column, no limit as None."""
    numbers, no_limit, switch = _collect_form()
    numbers["max_job_cost_usd"] = cast(ui.number, SimpleNamespace(value=7))
    no_limit["max_job_cost_usd"] = cast(ui.checkbox, SimpleNamespace(value=False))
    values = settings_panel._collect(numbers, no_limit, switch)

    assert values["airgapped"] is True
    assert isinstance(values["max_job_cost_usd"], float) and values["max_job_cost_usd"] == 7.0
    for column, *_rest, kind in settings_panel._FIELDS:
        if column == "max_job_cost_usd":
            continue
        if kind == "budget":
            assert values[column] is None
        elif kind == "int":
            assert isinstance(values[column], int) and values[column] == 3
        else:
            assert isinstance(values[column], float) and values[column] == 3.0


def test_a_budget_field_without_a_value_or_no_limit_is_rejected() -> None:
    """A budget without a value and without No limit ticked is not a valid form."""
    numbers, no_limit, switch = _collect_form()
    no_limit["max_project_spend_24h_usd"] = cast(ui.checkbox, SimpleNamespace(value=False))
    with pytest.raises(ValueError, match="Enter a value for Project spend"):
        settings_panel._collect(numbers, no_limit, switch)


def test_a_non_budget_field_without_a_value_is_rejected() -> None:
    """A setting that needs a value is not a valid form when it is empty."""
    numbers, no_limit, switch = _collect_form()
    numbers["agent_cpu"] = cast(ui.number, SimpleNamespace(value=None))
    with pytest.raises(ValueError, match="Enter a value for Agent CPU"):
        settings_panel._collect(numbers, no_limit, switch)


@pytest.mark.asyncio
async def test_a_load_failure_shows_an_error_banner_instead_of_the_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings_panel,
        "current_admin_settings",
        AsyncMock(side_effect=RuntimeError("database down")),
    )
    mock_ui = _panel_ui_mock()
    banner = MagicMock()
    with (
        patch.object(settings_panel, "ui", mock_ui),
        patch.object(settings_panel, "render_alert_banner", banner),
        patch.object(settings_panel, "render_empty_state", MagicMock()),
    ):
        await settings_panel.render_settings_panel()

    banner.assert_called_once()
    assert banner.call_args.kwargs["severity"] == "error"
    assert mock_ui.number.call_count == 0
    assert mock_ui.table.call_count == 0


@pytest.mark.asyncio
async def test_a_successful_save_stores_the_values_and_writes_history(
    db_session: AsyncSession,
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The row and user are committed on their own, so the panel's real reader
    # sees them, the way a running deployment sees the seeded row.
    user_id = await _commit_user(test_engine)
    await _commit_row(test_engine)
    user = User(email="admin@example.com", name="Admin", id=user_id)
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, False)
    mock_ui = _panel_ui_mock()
    with (
        patch.object(settings_panel, "ui", mock_ui),
        patch.object(settings_panel, "render_empty_state", MagicMock()),
        patch.object(settings_panel, "render_alert_banner", MagicMock()),
    ):
        await settings_panel.render_settings_panel()
        on_click = mock_ui.button.call_args.kwargs["on_click"]
        # The form holds the numbers behind each element's .classes() return.
        mock_ui.number.return_value.classes.return_value.value = 5
        mock_ui.checkbox.return_value.value = True
        mock_ui.switch.return_value.value = True
        await on_click()

    assert mock_ui.notify.call_args.kwargs["type"] == "positive"
    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.agent_cpu == 5.0
    assert row.agent_memory_mib == 5
    assert row.max_job_cost_usd is None
    assert row.airgapped is True
    assert row.updated_by == user.id
    pairs = set(await _change_pairs(db_session))
    assert ("Agent CPU", "2.0", "5.0") in pairs
    assert ("Job cost limit", "12.5", "no limit") in pairs
    assert ("Air-gap", "off", "on") in pairs


@pytest.mark.asyncio
async def test_a_missing_value_changes_nothing(
    db_session: AsyncSession,
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _commit_user(test_engine)
    await _commit_row(test_engine)
    user = User(email="admin@example.com", name="Admin", id=user_id)
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, False)
    mock_ui = _panel_ui_mock()
    with (
        patch.object(settings_panel, "ui", mock_ui),
        patch.object(settings_panel, "render_empty_state", MagicMock()),
        patch.object(settings_panel, "render_alert_banner", MagicMock()),
    ):
        await settings_panel.render_settings_panel()
        on_click = mock_ui.button.call_args.kwargs["on_click"]
        mock_ui.number.return_value.classes.return_value.value = None
        mock_ui.checkbox.return_value.value = False
        mock_ui.switch.return_value.value = False
        await on_click()

    assert mock_ui.notify.call_args.kwargs["type"] == "negative"
    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.agent_cpu == 2.0
    assert row.updated_by is None
    assert await _change_pairs(db_session) == []


@pytest.mark.asyncio
async def test_a_rejected_save_changes_nothing(
    db_session: AsyncSession,
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _commit_user(test_engine)
    await _commit_row(test_engine)
    user = User(email="admin@example.com", name="Admin", id=user_id)
    _admin(monkeypatch, db_session, user)
    _floor(monkeypatch, False)
    mock_ui = _panel_ui_mock()
    with (
        patch.object(settings_panel, "ui", mock_ui),
        patch.object(settings_panel, "render_empty_state", MagicMock()),
        patch.object(settings_panel, "render_alert_banner", MagicMock()),
    ):
        await settings_panel.render_settings_panel()
        on_click = mock_ui.button.call_args.kwargs["on_click"]
        # Every field at 0 passes the form, which the CHECK constraints refuse.
        mock_ui.number.return_value.classes.return_value.value = 0
        mock_ui.checkbox.return_value.value = True
        mock_ui.switch.return_value.value = True
        await on_click()

    assert mock_ui.notify.call_args.kwargs["type"] == "negative"
    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.agent_cpu == 2.0
    assert row.updated_by is None
    assert await _change_pairs(db_session) == []


@pytest.mark.asyncio
async def test_the_change_history_lists_each_change_with_its_changer(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="author@example.com", name="Author")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    db_session.add(
        AdminSettingChange(
            changed_at=datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
            changed_by=user.id,
            setting="Agent CPU",
            old_value="2.0",
            new_value="4.0",
        )
    )
    db_session.add(
        AdminSettingChange(
            changed_at=datetime(2026, 1, 2, 11, 0, tzinfo=UTC),
            changed_by=None,
            setting="Air-gap",
            old_value="off",
            new_value="on",
        )
    )
    await db_session.commit()

    monkeypatch.setattr(
        settings_panel,
        "current_admin_settings",
        AsyncMock(return_value=_panel_values(False)),
    )
    monkeypatch.setattr(settings_panel, "get_admin_session", fake_admin_session(db_session))
    mock_ui = _panel_ui_mock()
    with (
        patch.object(settings_panel, "ui", mock_ui),
        patch.object(settings_panel, "render_empty_state", MagicMock()),
        patch.object(settings_panel, "render_alert_banner", MagicMock()),
    ):
        await settings_panel.render_settings_panel()

    rows = mock_ui.table.call_args.kwargs["rows"]
    assert [row["who"] for row in rows] == ["Unknown", "author@example.com"]
    assert [row["setting"] for row in rows] == ["Air-gap", "Agent CPU"]
    assert [row["old"] for row in rows] == ["off", "2.0"]
    assert [row["new"] for row in rows] == ["on", "4.0"]


@pytest.mark.asyncio
async def test_an_empty_history_shows_the_empty_state(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings_panel,
        "current_admin_settings",
        AsyncMock(return_value=_panel_values(False)),
    )
    monkeypatch.setattr(settings_panel, "get_admin_session", fake_admin_session(db_session))
    empty = MagicMock()
    mock_ui = _panel_ui_mock()
    with (
        patch.object(settings_panel, "ui", mock_ui),
        patch.object(settings_panel, "render_empty_state", empty),
        patch.object(settings_panel, "render_alert_banner", MagicMock()),
    ):
        await settings_panel.render_settings_panel()

    empty.assert_called_once_with("No settings have been changed yet.")
    assert mock_ui.table.call_count == 0

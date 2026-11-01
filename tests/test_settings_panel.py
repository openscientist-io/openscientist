"""The admin Settings tab's save path, its guards, and the form's display."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.selectable import Select

from openscientist.admin_settings import AdminSettingsValues
from openscientist.database.models import AdminSettingChange, AdminSettings, User
from openscientist.webapp_components.pages import settings_panel
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


async def _write_row(db_session: AsyncSession) -> None:
    # The single row may already exist from startup seeding or an earlier test.
    await db_session.execute(delete(AdminSettings))
    db_session.add(AdminSettings(id=True, **_BASE))
    await db_session.commit()


_INSERT_ROW = """
    INSERT INTO admin_settings (
        max_job_cost_usd, max_project_spend_total_usd,
        max_project_spend_24h_usd, warn_project_spend_24h_usd,
        agent_cpu, executor_cpu, agent_memory_mib, executor_memory_mib,
        agent_timeout_seconds, executor_timeout_seconds,
        max_file_size_mb, max_concurrent_jobs, scheduler_window_hours,
        airgapped, session_duration_days
    ) VALUES (
        :max_job_cost_usd, :max_project_spend_total_usd,
        :max_project_spend_24h_usd, :warn_project_spend_24h_usd,
        :agent_cpu, :executor_cpu, :agent_memory_mib, :executor_memory_mib,
        :agent_timeout_seconds, :executor_timeout_seconds,
        :max_file_size_mb, :max_concurrent_jobs, :scheduler_window_hours,
        :airgapped, :session_duration_days
    )
"""


async def _commit_row(test_engine: AsyncEngine) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM admin_settings"))
        await conn.execute(text(_INSERT_ROW), dict(_BASE))


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
    try:
        success, message = await settings_panel.save_admin_settings(_values(agent_cpu=0.0))

        assert success is False
        assert "Agent CPU" in message
        row = await _row(db_session)
        await db_session.refresh(row)
        assert row.agent_cpu == 2.0
        assert row.updated_by is None
        assert await _change_pairs(db_session) == []
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM admin_settings"))


@pytest.mark.asyncio
async def test_the_airgap_switch_cannot_be_turned_off_while_the_floor_locks_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(email="admin@example.com", name="Admin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    row_values = dict(_BASE)
    row_values["airgapped"] = True
    db_session.add(AdminSettings(id=True, **row_values))
    await db_session.commit()
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

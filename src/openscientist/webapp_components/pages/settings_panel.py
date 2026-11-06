"""The admin Settings tab, its save path, and the change history."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from uuid import UUID

from nicegui import ui
from pydantic_settings import BaseSettings
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from openscientist.admin_settings import AdminSettingsValues, current_admin_settings
from openscientist.auth import get_current_user_id, is_current_user_admin
from openscientist.database.models import AdminSettingChange, AdminSettings, User
from openscientist.database.session import get_admin_session
from openscientist.settings import (
    AirgapSettings,
    AuthSettings,
    BudgetSettings,
    ContainerSettings,
    FileSettings,
    Settings,
    get_settings,
)
from openscientist.webapp_components.components.banners import render_alert_banner
from openscientist.webapp_components.components.common import render_empty_state
from openscientist.webapp_components.utils.permissions import (
    refuse_stale_admin_session,
    revalidate_admin_session,
)

logger = logging.getLogger(__name__)

# (column, label, unit, allowed range, kind) for every setting of the row.
_FIELDS: tuple[tuple[str, str, str, str, str], ...] = (
    ("max_job_cost_usd", "Job cost limit", "USD", "above 0 or no limit", "budget"),
    ("max_project_spend_total_usd", "Project spend total", "USD", "above 0 or no limit", "budget"),
    ("max_project_spend_24h_usd", "Project spend (24 h)", "USD", "above 0 or no limit", "budget"),
    (
        "warn_project_spend_24h_usd",
        "Project spend warning (24 h)",
        "USD",
        "above 0 or no limit",
        "budget",
    ),
    ("agent_cpu", "Agent CPU", "cores", "above 0", "float"),
    ("executor_cpu", "Executor CPU", "cores", "above 0", "float"),
    ("agent_memory_mib", "Agent memory", "MiB", "whole MiB, above 0", "int"),
    ("executor_memory_mib", "Executor memory", "MiB", "whole MiB, above 0", "int"),
    ("agent_timeout_seconds", "Agent timeout", "seconds", "above 0", "int"),
    ("executor_timeout_seconds", "Executor timeout", "seconds", "above 0", "int"),
    ("max_file_size_mb", "Max file size, per file and per request", "MB", "above 0", "int"),
    ("max_concurrent_jobs", "Max concurrent jobs", "", "0 or more, 0 pauses new starts", "int"),
    ("scheduler_window_hours", "Scheduler window", "hours", "above 0", "float"),
    ("session_duration_days", "Session length", "days", "1 or more", "int"),
)
_AIRGAP = ("airgapped", "Air-gap")

# The Font Awesome Free solid class for each field's prepend icon. Every
# _FIELDS column and the air-gap switch must have an entry.
_FIELD_ICONS: dict[str, str] = {
    "max_job_cost_usd": "fa-solid fa-sack-dollar",
    "max_project_spend_total_usd": "fa-solid fa-vault",
    "max_project_spend_24h_usd": "fa-solid fa-calendar-day",
    "warn_project_spend_24h_usd": "fa-solid fa-triangle-exclamation",
    "agent_cpu": "fa-solid fa-microchip",
    "executor_cpu": "fa-solid fa-microchip",
    "agent_memory_mib": "fa-solid fa-memory",
    "executor_memory_mib": "fa-solid fa-memory",
    "agent_timeout_seconds": "fa-solid fa-hourglass-half",
    "executor_timeout_seconds": "fa-solid fa-hourglass-half",
    "max_file_size_mb": "fa-solid fa-file-arrow-up",
    "max_concurrent_jobs": "fa-solid fa-layer-group",
    "scheduler_window_hours": "fa-solid fa-clock",
    "session_duration_days": "fa-solid fa-user-clock",
    "airgapped": "fa-solid fa-plug-circle-xmark",
}

# The vendored stylesheets the /admin page links, so Quasar renders the
# fa-solid names with the vendored webfont.
_FONTAWESOME_HEAD_HTML = (
    '<link rel="stylesheet" href="/fa/css/fontawesome.min.css">\n'
    '<link rel="stylesheet" href="/fa/css/solid.min.css">'
)

_LABELS = {column: label for column, label, *_ in (*_FIELDS, _AIRGAP)}
_INT_COLUMNS = {column for column, *_, kind in _FIELDS if kind == "int"}
_CHECK = re.compile(r"ck_admin_settings_([a-z0-9_]+)")

# (group title, columns) in the order the form shows them.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Budgets",
        (
            "max_job_cost_usd",
            "max_project_spend_total_usd",
            "max_project_spend_24h_usd",
            "warn_project_spend_24h_usd",
        ),
    ),
    (
        "Agent and executor",
        (
            "agent_cpu",
            "executor_cpu",
            "agent_memory_mib",
            "executor_memory_mib",
            "agent_timeout_seconds",
            "executor_timeout_seconds",
        ),
    ),
    ("Scheduling", ("max_concurrent_jobs", "scheduler_window_hours")),
    ("Files and session", ("max_file_size_mb", "session_duration_days")),
)

# The settings.py model and field each admin column maps to, so the panel can
# read the description instead of keeping a second copy of the wording.
_DESCRIPTION_SOURCES: dict[str, tuple[type[BaseSettings], str]] = {
    "max_job_cost_usd": (BudgetSettings, "max_job_cost_usd"),
    "max_project_spend_total_usd": (BudgetSettings, "max_project_spend_total_usd"),
    "max_project_spend_24h_usd": (BudgetSettings, "max_project_spend_24h_usd"),
    "warn_project_spend_24h_usd": (BudgetSettings, "warn_project_spend_24h_usd"),
    "agent_cpu": (ContainerSettings, "agent_cpu"),
    "executor_cpu": (ContainerSettings, "executor_cpu"),
    "agent_memory_mib": (ContainerSettings, "agent_memory"),
    "executor_memory_mib": (ContainerSettings, "executor_memory"),
    "agent_timeout_seconds": (ContainerSettings, "agent_timeout"),
    "executor_timeout_seconds": (ContainerSettings, "executor_timeout"),
    "max_file_size_mb": (FileSettings, "max_file_size_mb"),
    "max_concurrent_jobs": (Settings, "max_concurrent_jobs"),
    "scheduler_window_hours": (Settings, "scheduler_window_hours"),
    "session_duration_days": (AuthSettings, "session_duration_days"),
    "airgapped": (AirgapSettings, "enabled"),
}


def _description_for(column: str) -> str:
    """The settings.py description of the env field an admin column maps to."""
    model, field_name = _DESCRIPTION_SOURCES[column]
    return str(model.model_fields[field_name].description)


def _value_text(value: object) -> str:
    """The text form of a value, no limit for None and on or off for the switch."""
    if value is None:
        return "no limit"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _current_user_uuid() -> UUID | None:
    """The current user's id as a UUID, or None outside a request."""
    user_id = get_current_user_id()
    if user_id is None:
        return None
    return UUID(user_id)


_FIELD_BY_COLUMN = {column: (label, unit, rng, kind) for column, label, unit, rng, kind in _FIELDS}


def _render_setting_field(
    column: str,
    current: AdminSettingsValues,
    numbers: dict[str, ui.number],
    no_limit: dict[str, ui.checkbox],
) -> None:
    """One form field: value input, and one helper line with the description and the bound."""
    label, unit, rng, kind = _FIELD_BY_COLUMN[column]
    value = getattr(current, column)
    name = f"{label} ({unit})" if unit else label
    with ui.column().classes("min-w-0 gap-0"):
        if kind == "budget":
            field = ui.number(label=name, value=None if value is None else float(value), min=0)
        elif kind == "int":
            field = ui.number(label=name, value=value, min=0, step=1)
        else:
            field = ui.number(label=name, value=value, min=0)
        field.props("outlined dense")
        with field.add_slot("prepend"):
            ui.icon(_FIELD_ICONS[column])
        numbers[column] = field.classes("w-full")
        if kind == "budget":
            no_limit[column] = (
                ui.checkbox("No limit", value=value is None).props("dense").classes("mt-1")
            )
        ui.label(f"{_description_for(column)} ({rng})").classes("text-xs text-gray-500")


def _rejected_setting(exc: IntegrityError) -> str:
    """The label of the setting a CHECK violation names, or a fallback name."""
    match = _CHECK.search(str(exc))
    if match is None:
        return "A setting"
    return _LABELS.get(match[1], "A setting")


async def _save_settings(values: dict[str, object]) -> tuple[bool, str]:
    """Save the settings once the caller's admin session survives a re-check."""
    if not await revalidate_admin_session():
        refuse_stale_admin_session()
        return False, "Your admin session is no longer valid. Please refresh the page."
    return await save_admin_settings(values)


async def save_admin_settings(values: dict[str, object]) -> tuple[bool, str]:
    """Write the row and one history row per changed value, in one transaction."""
    if not is_current_user_admin():
        logger.warning("Non-admin attempt to save admin settings blocked")
        return False, "You are not authorized to save settings."

    floor = get_settings().airgap.enabled
    if floor and not values.get("airgapped", True):
        return False, "Air-gap is forced on by the environment and cannot be turned off."

    changed_by = _current_user_uuid()
    async with get_admin_session() as session:
        row = (
            await session.execute(select(AdminSettings).where(AdminSettings.id.is_(True)))
        ).scalar_one_or_none()
        if row is None:
            return False, "The admin settings row is missing. The web seeds it at startup."

        # The floor owns the switch, so a locked form never edits the row's value.
        fields = [field for field in (*_FIELDS, _AIRGAP) if not (floor and field[0] == "airgapped")]
        changes: list[AdminSettingChange] = []
        for column, label, *_ in fields:
            old = getattr(row, column)
            new = values.get(column, old)
            if new == old:
                continue
            setattr(row, column, new)
            changes.append(
                AdminSettingChange(
                    changed_by=changed_by,
                    setting=label,
                    old_value=_value_text(old),
                    new_value=_value_text(new),
                )
            )
        if not changes:
            return True, "No changes to save."

        row.updated_at = datetime.now(UTC)
        row.updated_by = changed_by
        session.add_all(changes)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.error("Admin settings save rejected by the database: %s", exc, exc_info=True)
            return False, f"{_rejected_setting(exc)} is outside its allowed range."

    # A changed limit may release a held queue, so a save picks once at once,
    # off this request's loop.
    from openscientist.web_app import get_job_manager

    try:
        await asyncio.to_thread(get_job_manager()._start_next_queued_job)
    except Exception as e:
        logger.warning("Could not re-pick the queue after saving settings: %s", e)

    return True, "Settings saved."


def _collect(
    numbers: dict[str, ui.number],
    no_limit: dict[str, ui.checkbox],
    switch: ui.switch,
) -> dict[str, object]:
    """The form's values keyed by column, raising ValueError naming a missing one."""
    values: dict[str, object] = {}
    for column, label, *_ in _FIELDS:
        raw = numbers[column].value
        if column in no_limit:
            if no_limit[column].value:
                values[column] = None
            elif raw is None:
                raise ValueError(f"Enter a value for {label} or tick No limit.")
            else:
                values[column] = float(raw)
        elif raw is None:
            raise ValueError(f"Enter a value for {label}.")
        else:
            values[column] = int(raw) if column in _INT_COLUMNS else float(raw)
    values["airgapped"] = bool(switch.value)
    return values


async def render_settings_panel() -> None:
    """Render the Settings tab with the form and the change history."""
    container = ui.column().classes("w-full gap-6")
    await _render_settings(container)


async def _render_settings(container: ui.column) -> None:
    with container:
        try:
            current = await current_admin_settings()
        except Exception as exc:
            logger.error("Failed to load admin settings: %s", exc, exc_info=True)
            render_alert_banner(
                title="Error",
                message="Failed to load settings. Check server logs.",
                severity="error",
            )
            return

        with ui.card().classes("w-full p-4"):
            ui.label("Settings").classes("text-lg font-bold mb-2")
            numbers: dict[str, ui.number] = {}
            no_limit: dict[str, ui.checkbox] = {}
            for group_title, columns in _GROUPS:
                ui.label(group_title).classes("text-subtitle2 font-bold text-gray-700 mt-4")
                # As many columns as fit, each at least 15rem wide.
                with (
                    ui.element("div")
                    .classes("w-full gap-x-6 gap-y-3")
                    .style(
                        "display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr))"
                    )
                ):
                    for column in columns:
                        _render_setting_field(column, current, numbers, no_limit)

            ui.label("Connection").classes("text-subtitle2 font-bold text-gray-700 mt-4")
            with ui.row().classes("w-full items-start gap-4"):
                switch = ui.switch(value=current.airgapped)
                with switch:
                    # The block-level label would wrap below the icon, so the
                    # two share one inline row, centred with the toggle.
                    with ui.row().classes("items-center gap-1"):
                        ui.icon(_FIELD_ICONS[_AIRGAP[0]])
                        ui.label("Air-gap")
                with ui.column().classes("gap-0"):
                    ui.label(_description_for("airgapped")).classes("text-xs text-gray-500")
                    if current.airgap_locked:
                        switch.props("disable")
                        ui.label(
                            "Locked on because OPENSCIENTIST_AIRGAPPED is set in the environment."
                        ).classes("text-xs text-gray-500")

            async def save() -> None:
                try:
                    values = _collect(numbers, no_limit, switch)
                except ValueError as exc:
                    ui.notify(str(exc), type="negative")
                    return
                success, message = await _save_settings(values)
                ui.notify(message, type="positive" if success else "negative")
                if success:
                    container.clear()
                    await _render_settings(container)

            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("Save", icon="save", on_click=save).props("color=primary")

        history = ui.column().classes("w-full")
        await _load_history(history)


async def _load_history(container: ui.column) -> None:
    with container:
        ui.label("Change history").classes("text-lg font-bold")
        try:
            async with get_admin_session() as session:
                stmt = (
                    select(AdminSettingChange, User)
                    .outerjoin(User, User.id == AdminSettingChange.changed_by)
                    .order_by(
                        AdminSettingChange.changed_at.desc(),
                        AdminSettingChange.id.desc(),
                    )
                    .limit(100)
                )
                rows = (await session.execute(stmt)).all()
        except Exception as exc:
            logger.error("Failed to load the settings history: %s", exc, exc_info=True)
            render_alert_banner(
                title="Error",
                message="Failed to load the change history. Check server logs.",
                severity="error",
            )
            return

        if not rows:
            render_empty_state("No settings have been changed yet.")
            return

        table_rows = [
            {
                "id": str(change.id),
                "when": change.changed_at.strftime("%Y-%m-%d %H:%M"),
                "who": _who(user),
                "setting": change.setting,
                "old": change.old_value or "",
                "new": change.new_value or "",
            }
            for change, user in rows
        ]
        ui.table(
            columns=[
                {"name": "when", "label": "When", "field": "when", "align": "left"},
                {"name": "who", "label": "Who", "field": "who", "align": "left"},
                {"name": "setting", "label": "Setting", "field": "setting", "align": "left"},
                {"name": "old", "label": "Old", "field": "old", "align": "left"},
                {"name": "new", "label": "New", "field": "new", "align": "left"},
            ],
            rows=table_rows,
            row_key="id",
        ).classes("w-full")


def _who(user: User | None) -> str:
    """The email or name a history row shows, Unknown if the user is gone."""
    if user is None:
        return "Unknown"
    if user.email:
        return user.email
    return user.name or "Unknown"

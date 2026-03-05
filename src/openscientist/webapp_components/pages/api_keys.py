"""API Keys management page."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from openscientist.api.auth import generate_api_key_secret, hash_secret
from openscientist.auth import get_current_user_id, is_current_user_admin, require_auth
from openscientist.database.models import APIKey, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_admin_session, get_session_ctx
from openscientist.webapp_components.ui_components import (
    format_relative_time,
    render_dialog_actions,
    render_empty_state,
    render_navigator,
)
from openscientist.webapp_components.utils import setup_timer_cleanup

logger = logging.getLogger(__name__)

MAX_KEYS_PER_USER = 10

_STATUS_SLOT = r"""
<q-td :props="props">
    <q-badge :color="props.row.status_color" outline>
        {{ props.row.status }}
    </q-badge>
</q-td>
"""

_ACTIONS_SLOT = r"""
<q-td :props="props">
    <q-btn
        v-if="props.row.is_active"
        flat
        dense
        color="negative"
        icon="delete"
        label="Revoke"
        @click="$parent.$emit('revoke', props.row)"
    />
    <span v-else class="text-grey">-</span>
</q-td>
"""


def _api_key_columns(is_admin: bool) -> list[dict[str, Any]]:
    """Return table column definitions, including User column for admins."""
    columns = [
        {"name": "name", "label": "Name", "field": "name", "align": "left", "sortable": True},
    ]
    if is_admin:
        columns.append(
            {"name": "user", "label": "User", "field": "user", "align": "left", "sortable": True}
        )
    columns.extend(
        [
            {"name": "created_at", "label": "Created", "field": "created_at", "align": "left"},
            {
                "name": "last_used_at",
                "label": "Last Used",
                "field": "last_used_at",
                "align": "left",
            },
            {
                "name": "usage_count",
                "label": "Uses",
                "field": "usage_count",
                "align": "center",
                "sortable": True,
            },
            {"name": "status", "label": "Status", "field": "status", "align": "center"},
            {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
        ]
    )
    return columns


def _user_display(user: User | None) -> tuple[str, str]:
    """Build user display labels for admin table rows."""
    if not user:
        return "", ""
    if user.name and user.email:
        return f"{user.name} ({user.email})", user.name
    return user.name or user.email, user.name or ""


def _api_key_rows(api_keys: list[APIKey]) -> list[dict[str, Any]]:
    """Serialize API keys for table rows."""
    rows: list[dict[str, Any]] = []
    for api_key in api_keys:
        user_label, user_name = _user_display(api_key.user)
        rows.append(
            {
                "id": str(api_key.id),
                "name": api_key.name,
                "user": user_label,
                "user_name": user_name,
                "created_at": format_relative_time(api_key.created_at),
                "last_used_at": format_relative_time(api_key.last_used_at)
                if api_key.last_used_at
                else "Never",
                "usage_count": api_key.usage_count,
                "is_active": api_key.is_active,
                "status": "Active" if api_key.is_active else "Revoked",
                "status_color": "positive" if api_key.is_active else "grey",
            }
        )
    return rows


def _normalize_key_name(raw_name: str | None) -> str:
    """Return normalized key name."""
    return (raw_name or "").strip()


def _validate_key_name(name: str) -> str | None:
    """Validate key name and return error message, if any."""
    if not name:
        return "Key name is required"
    if len(name) > 100:
        return "Key name must be 100 characters or less"
    return None


async def _check_user_key_limit(session: Any, user_uuid: UUID) -> bool:
    """Return True if user already reached key limit."""
    result = await session.execute(select(func.count()).where(APIKey.user_id == user_uuid))
    return (result.scalar() or 0) >= MAX_KEYS_PER_USER


async def _key_name_exists(session: Any, user_uuid: UUID, key_name: str) -> bool:
    """Return True when same key name already exists for user."""
    result = await session.execute(
        select(APIKey).where(APIKey.user_id == user_uuid, APIKey.name == key_name)
    )
    return result.scalar_one_or_none() is not None


async def _create_key_for_user(user_id: str, key_name: str) -> str:
    """Create API key and return plaintext key to display exactly once."""
    user_uuid = UUID(user_id)
    async with get_session_ctx() as session:
        await set_current_user(session, user_uuid)
        if await _check_user_key_limit(session, user_uuid):
            raise ValueError(f"Maximum {MAX_KEYS_PER_USER} API keys allowed")
        if await _key_name_exists(session, user_uuid, key_name):
            raise ValueError("A key with this name already exists")

        secret = generate_api_key_secret()
        full_key = f"{key_name}:{secret}"
        session.add(APIKey(user_id=user_uuid, name=key_name, key_hash=hash_secret(secret)))
        await session.commit()
    return full_key


async def _revoke_key(api_key_id: str, requester_id: str, is_admin: bool) -> bool:
    """Revoke API key by id, enforcing ownership for non-admin users."""
    key_uuid = UUID(api_key_id)
    requester_uuid = UUID(requester_id)

    if is_admin:
        async with get_admin_session() as session:
            result = await session.execute(select(APIKey).where(APIKey.id == key_uuid))
            api_key = result.scalar_one_or_none()
            if not api_key:
                return False
            api_key.is_active = False
            await session.commit()
            return True

    async with get_session_ctx() as session:
        await set_current_user(session, requester_uuid)
        result = await session.execute(
            select(APIKey).where(APIKey.id == key_uuid, APIKey.user_id == requester_uuid)
        )
        api_key = result.scalar_one_or_none()
        if not api_key:
            return False
        api_key.is_active = False
        await session.commit()
        return True


async def _query_api_keys(search_query: str, is_admin: bool, user_id: str) -> list[APIKey]:
    """Query API keys according to role and search string."""
    normalized = search_query.strip().lower()
    if is_admin:
        async with get_admin_session() as session:
            stmt = (
                select(APIKey).options(selectinload(APIKey.user)).order_by(APIKey.created_at.desc())
            )
            if normalized:
                pattern = f"%{normalized}%"
                stmt = stmt.join(User).where(
                    or_(
                        APIKey.name.ilike(pattern),
                        User.name.ilike(pattern),
                        User.email.ilike(pattern),
                    )
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    user_uuid = UUID(user_id)
    async with get_session_ctx() as session:
        await set_current_user(session, user_uuid)
        stmt = (
            select(APIKey)
            .options(selectinload(APIKey.user))
            .where(APIKey.user_id == user_uuid)
            .order_by(APIKey.created_at.desc())
        )
        if normalized:
            stmt = stmt.where(APIKey.name.ilike(f"%{normalized}%"))
        result = await session.execute(stmt)
        return list(result.scalars().all())


def _render_api_keys_table(
    container: ui.element,
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    on_revoke: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    """Render API keys table with status and revoke slots."""
    with container:
        table = ui.table(columns=columns, rows=rows, row_key="id", pagination=20).classes("w-full")
        table.add_slot("body-cell-status", _STATUS_SLOT)
        table.add_slot("body-cell-actions", _ACTIONS_SLOT)
        table.on("revoke", lambda event: on_revoke(event.args))


async def _show_key_created_dialog(full_key: str) -> None:
    """Show one-time dialog containing newly generated API key."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
        ui.label("API Key Created").classes("text-lg font-bold mb-2")
        with ui.row().classes("w-full bg-amber-100 border-l-4 border-amber-500 p-3 mb-4"):
            ui.icon("warning", color="amber-700").classes("mr-2")
            ui.label("Copy this key now. You won't be able to see it again!").classes(
                "text-amber-800"
            )
        with ui.row().classes("w-full gap-2 items-center"):
            key_display = ui.input(value=full_key).classes("flex-grow")
            key_display.props("readonly outlined dense")

            async def copy_key() -> None:
                await ui.run_javascript(f"navigator.clipboard.writeText({full_key!r})")
                ui.notify("Key copied to clipboard", type="positive")

            ui.button(icon="content_copy", on_click=copy_key).props("flat color=primary").tooltip(
                "Copy to clipboard"
            )
        ui.markdown(
            "Use this key in the `Authorization` header:\n\n"
            "```\nAuthorization: Bearer <name>:<secret>\n```"
        ).classes("mt-4 text-sm text-gray-600")
        with ui.row().classes("w-full justify-end mt-4"):
            ui.button("Done", on_click=dialog.close).props("color=primary")
    dialog.open()


def _open_revoke_dialog(
    *,
    api_key_id: str,
    api_key_name: str,
    owner_name: str,
    is_admin: bool,
    on_revoked: Callable[[], Awaitable[None]],
) -> None:
    """Open confirmation dialog for key revocation."""

    async def do_revoke() -> None:
        try:
            requester_id = get_current_user_id()
            if not requester_id:
                ui.notify("You must be signed in to revoke API keys", type="negative")
                dialog.close()
                return
            success = await _revoke_key(api_key_id, requester_id, is_admin)
            if not success:
                ui.notify("API key not found", type="negative")
                dialog.close()
                return
            ui.notify("API key revoked", type="positive")
            dialog.close()
            await on_revoked()
        except Exception as exc:
            logger.error("Failed to revoke API key: %s", exc, exc_info=True)
            ui.notify("Failed to revoke key. Please try again.", type="negative")

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Revoke API Key").classes("text-lg font-bold mb-2")
        if is_admin and owner_name:
            ui.label(
                f'Are you sure you want to revoke the key "{api_key_name}" owned by {owner_name}?'
            ).classes("mb-2")
        else:
            ui.label(f'Are you sure you want to revoke the key "{api_key_name}"?').classes("mb-2")
        ui.label(
            "This action cannot be undone. Any applications using this key "
            "will no longer be able to authenticate."
        ).classes("text-sm text-gray-600 mb-4")
        render_dialog_actions(
            on_confirm=do_revoke,
            on_cancel=dialog.close,
            confirm_label="Revoke",
            confirm_props="color=negative",
        )
    dialog.open()


def _api_keys_intro(is_admin: bool) -> str:
    """Return intro markdown text for API keys page."""
    admin_prefix = "**Admin view**: You can see all API keys across all users. " if is_admin else ""
    return (
        admin_prefix + "API keys allow programmatic access to the OpenScientist REST API. "
        "Keys use the format `name:secret` for authentication."
    )


def _search_placeholder(is_admin: bool) -> str:
    """Return search placeholder text based on role."""
    if is_admin:
        return "Search by key name or user..."
    return "Search by key name..."


def _make_create_key_handler(
    *,
    key_name_input: ui.input,
    user_id: str,
    load_keys: Callable[[], Awaitable[None]],
) -> Callable[[], Awaitable[None]]:
    """Build create-key callback bound to page state."""

    async def create_key() -> None:
        key_name = _normalize_key_name(key_name_input.value)
        error = _validate_key_name(key_name)
        if error:
            ui.notify(error, type="negative")
            return
        try:
            full_key = await _create_key_for_user(user_id, key_name)
        except ValueError as exc:
            ui.notify(str(exc), type="negative")
            return
        except Exception as exc:
            logger.error("Failed to create API key: %s", exc, exc_info=True)
            ui.notify("Failed to create key. Please try again.", type="negative")
            return
        await _show_key_created_dialog(full_key)
        key_name_input.value = ""
        await load_keys()

    return create_key


def _make_load_keys_handler(
    *,
    keys_table_container: ui.element,
    state: dict[str, str],
    is_admin: bool,
    user_id: str,
) -> Callable[[], Awaitable[None]]:
    """Build load-keys callback bound to current page elements."""

    async def load_keys() -> None:
        keys_table_container.clear()
        try:
            api_keys = await _query_api_keys(state["search"], is_admin, user_id)
        except Exception as exc:
            logger.error("Failed to load API keys: %s", exc, exc_info=True)
            with keys_table_container:
                ui.label("Failed to load API keys. Check server logs.").classes("text-red-500")
            return

        if not api_keys:
            with keys_table_container:
                if state["search"].strip():
                    render_empty_state("No API keys match your search.")
                else:
                    render_empty_state("No API keys yet. Create one to get started.")
            return

        rows = _api_key_rows(api_keys)
        columns = _api_key_columns(is_admin)

        async def handle_revoke(row: dict[str, Any]) -> None:
            _open_revoke_dialog(
                api_key_id=row["id"],
                api_key_name=row["name"],
                owner_name=row.get("user_name", ""),
                is_admin=is_admin,
                on_revoked=load_keys,
            )

        _render_api_keys_table(keys_table_container, rows, columns, handle_revoke)

    return load_keys


@ui.page("/api-keys")
@require_auth
async def api_keys_page() -> None:
    """API Keys management page."""
    ui.page_title("API Keys - OpenScientist")
    _active_timers = setup_timer_cleanup()
    is_admin = is_current_user_admin()
    render_navigator(active_page="api-keys")

    state = {"search": ""}
    user_id = get_current_user_id()
    assert user_id is not None

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-6"):
        ui.markdown("# API Keys")
        ui.markdown(_api_keys_intro(is_admin))

        with ui.row().classes("w-full items-center gap-2 text-sm text-gray-600"):
            ui.icon("info", size="sm").classes("text-blue-500")
            ui.markdown(
                "See the [API Reference](/docs#api-reference) for endpoint documentation, "
                "or try the interactive [Swagger UI](/api-docs) and [ReDoc](/api-redoc)."
            ).classes("m-0")

        with ui.card().classes("w-full"):
            ui.label("Create New API Key").classes("text-lg font-bold mb-2")
            with ui.row().classes("w-full gap-4 items-end"):
                key_name_input = ui.input(
                    label="Key Name",
                    placeholder="e.g., my-script, ci-pipeline",
                    validation={
                        "Required": lambda value: bool(value and value.strip()),
                        "Max 100 characters": lambda value: len(value) <= 100 if value else True,
                    },
                ).classes("flex-grow")
                key_name_input.props("outlined dense")
                create_key = _make_create_key_handler(
                    key_name_input=key_name_input,
                    user_id=user_id,
                    load_keys=lambda: load_keys(),
                )
                ui.button("Create Key", icon="add", on_click=create_key).props("color=primary")

        with ui.row().classes("w-full gap-4 items-end"):
            search_input = ui.input(
                label="Search",
                placeholder=_search_placeholder(is_admin),
            ).classes("flex-grow")
            search_input.props("clearable outlined dense")

        keys_table_container = ui.column().classes("w-full")
        load_keys = _make_load_keys_handler(
            keys_table_container=keys_table_container,
            state=state,
            is_admin=is_admin,
            user_id=user_id,
        )

        async def on_search_change(event: Any) -> None:
            state["search"] = event.value or ""
            await load_keys()

        search_input.on("update:model-value", on_search_change)
        _active_timers.append(ui.timer(0.1, load_keys, once=True))

"""API Keys management page."""

import logging
from uuid import UUID

from nicegui import ui
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from shandy.api.auth import generate_api_key_secret, hash_secret
from shandy.auth import get_current_user_id, is_current_user_admin, require_auth
from shandy.database.models import APIKey, User
from shandy.database.rls import set_current_user
from shandy.database.session import AsyncSessionLocal, get_admin_session
from shandy.webapp_components.ui_components import (
    format_relative_time,
    render_dialog_actions,
    render_empty_state,
    render_navigator,
)
from shandy.webapp_components.utils import setup_timer_cleanup

logger = logging.getLogger(__name__)

# Maximum API keys per user
MAX_KEYS_PER_USER = 10


@ui.page("/api-keys")
@require_auth
async def api_keys_page():
    """API Keys management page."""
    ui.page_title("API Keys - SHANDY")

    # Track active timers for cleanup on disconnect
    _active_timers = setup_timer_cleanup()

    # Check if current user is admin
    is_admin = is_current_user_admin()

    # Page header with navigation
    render_navigator(active_page="api-keys")

    # State for search
    state = {"search": ""}

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-6"):
        ui.markdown("# API Keys")
        if is_admin:
            ui.markdown(
                "**Admin view**: You can see all API keys across all users. "
                "API keys allow programmatic access to the SHANDY REST API. "
                "Keys use the format `name:secret` for authentication."
            )
        else:
            ui.markdown(
                "API keys allow programmatic access to the SHANDY REST API. "
                "Keys use the format `name:secret` for authentication."
            )

        # Documentation links
        with ui.row().classes("w-full items-center gap-2 text-sm text-gray-600"):
            ui.icon("info", size="sm").classes("text-blue-500")
            ui.markdown(
                "See the [API Reference](/docs#api-reference) for endpoint documentation, "
                "or try the interactive [Swagger UI](/api-docs) and [ReDoc](/api-redoc)."
            ).classes("m-0")

        # Create key card
        with ui.card().classes("w-full"):
            ui.label("Create New API Key").classes("text-lg font-bold mb-2")

            with ui.row().classes("w-full gap-4 items-end"):
                key_name_input = ui.input(
                    label="Key Name",
                    placeholder="e.g., my-script, ci-pipeline",
                    validation={
                        "Required": lambda v: bool(v and v.strip()),
                        "Max 100 characters": lambda v: len(v) <= 100 if v else True,
                    },
                ).classes("flex-grow")
                key_name_input.props("outlined dense")

                async def create_key():
                    """Create a new API key."""
                    name = key_name_input.value
                    if not name or not name.strip():
                        ui.notify("Key name is required", type="negative")
                        return

                    name = name.strip()
                    if len(name) > 100:
                        ui.notify("Key name must be 100 characters or less", type="negative")
                        return

                    try:
                        user_id = get_current_user_id()
                        async with AsyncSessionLocal() as session:
                            await set_current_user(session, UUID(user_id))

                            # Check key limit
                            count_stmt = select(func.count()).where(APIKey.user_id == UUID(user_id))
                            result = await session.execute(count_stmt)
                            key_count = result.scalar() or 0

                            if key_count >= MAX_KEYS_PER_USER:
                                ui.notify(
                                    f"Maximum {MAX_KEYS_PER_USER} API keys allowed",
                                    type="negative",
                                )
                                return

                            # Check for duplicate name
                            dupe_stmt = select(APIKey).where(
                                APIKey.user_id == UUID(user_id),
                                APIKey.name == name,
                            )
                            result = await session.execute(dupe_stmt)
                            if result.scalar_one_or_none():
                                ui.notify(
                                    "A key with this name already exists",
                                    type="negative",
                                )
                                return

                            # Generate key
                            secret = generate_api_key_secret()
                            full_key = f"{name}:{secret}"
                            key_hash = hash_secret(secret)

                            # Create API key record
                            api_key = APIKey(
                                user_id=UUID(user_id),
                                name=name,
                                key_hash=key_hash,
                            )
                            session.add(api_key)
                            await session.commit()

                        # Show success dialog with full key
                        await show_key_created_dialog(full_key)

                        # Clear input and refresh table
                        key_name_input.value = ""
                        await load_keys()

                    except Exception as e:
                        logger.error("Failed to create API key: %s", e, exc_info=True)
                        ui.notify("Failed to create key. Please try again.", type="negative")

                ui.button(
                    "Create Key",
                    icon="add",
                    on_click=create_key,
                ).props("color=primary")

        # Search box (shown for admins or if user has keys)
        with ui.row().classes("w-full gap-4 items-end"):
            search_input = ui.input(
                label="Search",
                placeholder=(
                    "Search by key name or user..." if is_admin else "Search by key name..."
                ),
            ).classes("flex-grow")
            search_input.props("clearable outlined dense")

            async def on_search_change(e):
                state["search"] = e.value or ""
                await load_keys()

            search_input.on("update:model-value", on_search_change)

        # Keys table container
        keys_table_container = ui.column().classes("w-full")

        async def show_key_created_dialog(full_key: str):
            """Show dialog with the newly created key."""
            with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
                ui.label("API Key Created").classes("text-lg font-bold mb-2")

                # Warning banner
                with ui.row().classes("w-full bg-amber-100 border-l-4 border-amber-500 p-3 mb-4"):
                    ui.icon("warning", color="amber-700").classes("mr-2")
                    ui.label("Copy this key now. You won't be able to see it again!").classes(
                        "text-amber-800"
                    )

                # Key display
                with ui.row().classes("w-full gap-2 items-center"):
                    key_display = ui.input(value=full_key).classes("flex-grow")
                    key_display.props("readonly outlined dense")

                    async def copy_key():
                        await ui.run_javascript(f"navigator.clipboard.writeText({repr(full_key)})")
                        ui.notify("Key copied to clipboard", type="positive")

                    ui.button(icon="content_copy", on_click=copy_key).props(
                        "flat color=primary"
                    ).tooltip("Copy to clipboard")

                ui.markdown(
                    "Use this key in the `Authorization` header:\n\n"
                    "```\nAuthorization: Bearer <name>:<secret>\n```"
                ).classes("mt-4 text-sm text-gray-600")

                with ui.row().classes("w-full justify-end mt-4"):
                    ui.button("Done", on_click=dialog.close).props("color=primary")

            dialog.open()

        async def show_revoke_dialog(api_key_id: str, api_key_name: str, owner_name: str):
            """Show confirmation dialog for revoking a key."""

            async def do_revoke():
                try:
                    user_id = get_current_user_id()

                    # Admins use admin session, users use RLS session
                    if is_admin:
                        async with get_admin_session() as session:
                            stmt = select(APIKey).where(APIKey.id == UUID(api_key_id))
                            result = await session.execute(stmt)
                            api_key = result.scalar_one_or_none()

                            if not api_key:
                                ui.notify("API key not found", type="negative")
                                dialog.close()
                                return

                            api_key.is_active = False
                            await session.commit()
                    else:
                        async with AsyncSessionLocal() as session:
                            await set_current_user(session, UUID(user_id))

                            stmt = select(APIKey).where(
                                APIKey.id == UUID(api_key_id),
                                APIKey.user_id == UUID(user_id),
                            )
                            result = await session.execute(stmt)
                            api_key = result.scalar_one_or_none()

                            if not api_key:
                                ui.notify("API key not found", type="negative")
                                dialog.close()
                                return

                            api_key.is_active = False
                            await session.commit()

                    ui.notify("API key revoked", type="positive")
                    dialog.close()
                    await load_keys()

                except Exception as e:
                    logger.error("Failed to revoke API key: %s", e, exc_info=True)
                    ui.notify("Failed to revoke key. Please try again.", type="negative")

            with ui.dialog() as dialog, ui.card().classes("w-96"):
                ui.label("Revoke API Key").classes("text-lg font-bold mb-2")
                if is_admin and owner_name:
                    ui.label(
                        f'Are you sure you want to revoke the key "{api_key_name}" '
                        f"owned by {owner_name}?"
                    ).classes("mb-2")
                else:
                    ui.label(f'Are you sure you want to revoke the key "{api_key_name}"?').classes(
                        "mb-2"
                    )
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

        async def load_keys():
            """Load and display API keys."""
            keys_table_container.clear()

            try:
                user_id = get_current_user_id()
                search_query = state["search"].strip().lower()

                # Build the query based on admin status
                if is_admin:
                    # Admin: query all keys with user info
                    async with get_admin_session() as session:
                        stmt = (
                            select(APIKey)
                            .options(selectinload(APIKey.user))
                            .order_by(APIKey.created_at.desc())
                        )

                        # Apply search filter
                        if search_query:
                            stmt = stmt.join(User).where(
                                or_(
                                    APIKey.name.ilike(f"%{search_query}%"),
                                    User.name.ilike(f"%{search_query}%"),
                                    User.email.ilike(f"%{search_query}%"),
                                )
                            )

                        result = await session.execute(stmt)
                        api_keys = result.scalars().all()
                else:
                    # Regular user: only their keys
                    async with AsyncSessionLocal() as session:
                        await set_current_user(session, UUID(user_id))

                        stmt = (
                            select(APIKey)
                            .options(selectinload(APIKey.user))
                            .where(APIKey.user_id == UUID(user_id))
                            .order_by(APIKey.created_at.desc())
                        )

                        # Apply search filter
                        if search_query:
                            stmt = stmt.where(APIKey.name.ilike(f"%{search_query}%"))

                        result = await session.execute(stmt)
                        api_keys = result.scalars().all()

                if not api_keys:
                    with keys_table_container:
                        if search_query:
                            render_empty_state("No API keys match your search.")
                        else:
                            render_empty_state("No API keys yet. Create one to get started.")
                    return

                # Build table rows
                rows = []
                for key in api_keys:
                    user_display = ""
                    if key.user:
                        user_display = key.user.name or key.user.email
                        if key.user.name and key.user.email:
                            user_display = f"{key.user.name} ({key.user.email})"

                    rows.append(
                        {
                            "id": str(key.id),
                            "name": key.name,
                            "user": user_display,
                            "user_name": key.user.name if key.user else "",
                            "created_at": format_relative_time(key.created_at),
                            "last_used_at": (
                                format_relative_time(key.last_used_at)
                                if key.last_used_at
                                else "Never"
                            ),
                            "usage_count": key.usage_count,
                            "is_active": key.is_active,
                            "status": "Active" if key.is_active else "Revoked",
                            "status_color": "positive" if key.is_active else "grey",
                        }
                    )

                # Define columns based on admin status
                columns = [
                    {
                        "name": "name",
                        "label": "Name",
                        "field": "name",
                        "align": "left",
                        "sortable": True,
                    },
                ]

                # Add user column for admins
                if is_admin:
                    columns.append(
                        {
                            "name": "user",
                            "label": "User",
                            "field": "user",
                            "align": "left",
                            "sortable": True,
                        }
                    )

                columns.extend(
                    [
                        {
                            "name": "created_at",
                            "label": "Created",
                            "field": "created_at",
                            "align": "left",
                        },
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
                        {
                            "name": "status",
                            "label": "Status",
                            "field": "status",
                            "align": "center",
                        },
                        {
                            "name": "actions",
                            "label": "Actions",
                            "field": "actions",
                            "align": "center",
                        },
                    ]
                )

                with keys_table_container:
                    table = ui.table(
                        columns=columns,
                        rows=rows,
                        row_key="id",
                        pagination=20,
                    ).classes("w-full")

                    # Status badge slot
                    table.add_slot(
                        "body-cell-status",
                        r"""
                        <q-td :props="props">
                            <q-badge :color="props.row.status_color" outline>
                                {{ props.row.status }}
                            </q-badge>
                        </q-td>
                        """,
                    )

                    # Actions slot with revoke button (only for active keys)
                    table.add_slot(
                        "body-cell-actions",
                        r"""
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
                        """,
                    )

                    # Handle revoke button click
                    async def handle_revoke(e):
                        await show_revoke_dialog(
                            e.args["id"],
                            e.args["name"],
                            e.args.get("user_name", ""),
                        )

                    table.on("revoke", handle_revoke)

            except Exception as e:
                logger.error("Failed to load API keys: %s", e, exc_info=True)
                with keys_table_container:
                    ui.label("Failed to load API keys. Check server logs.").classes("text-red-500")

        # Initial load
        async def initial_load():
            await load_keys()

        init_timer = ui.timer(0.1, initial_load, once=True)
        _active_timers.append(init_timer)

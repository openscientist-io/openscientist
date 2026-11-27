"""Admin panel registering remote MCP servers in the catalog, all inactive."""

import logging
from typing import Any
from uuid import UUID

from nicegui import ui

from openscientist.admin.mcp_servers import (
    McpServerRegistration,
    list_mcp_servers,
    parse_registration,
    register_mcp_server,
)
from openscientist.database.models import McpServer
from openscientist.webapp_components.components.banners import render_alert_banner
from openscientist.webapp_components.components.common import render_empty_state
from openscientist.webapp_components.utils import in_page_client
from openscientist.webapp_components.utils.permissions import admin_access

logger = logging.getLogger(__name__)

_COLUMNS: list[dict[str, Any]] = [
    {"name": "slug", "label": "Slug", "field": "slug", "align": "left"},
    {"name": "display_name", "label": "Display name", "field": "display_name", "align": "left"},
    {"name": "url", "label": "URL", "field": "url", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "align": "center"},
    {"name": "created_at", "label": "Registered", "field": "created_at", "align": "left"},
]


def _rows(servers: list[McpServer]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(server.id),
            "slug": server.slug,
            "display_name": server.display_name,
            "url": server.url,
            "status": "Active" if server.is_active else "Inactive",
            "created_at": server.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for server in servers
    ]


async def render_mcp_servers_panel(page_user_id: str) -> None:
    """Render the MCP server catalog for the admin ``page_user_id``, whom new
    registrations name as their registrant."""
    with ui.column().classes("w-full gap-4"):
        ui.markdown("## MCP Servers")
        ui.label(
            "Registered servers stay inactive: no job uses them, and OpenScientist "
            "never contacts them."
        ).classes("text-sm text-gray-600")

        with ui.card().classes("w-full"):
            ui.label("Register MCP Server").classes("text-lg font-bold mb-2")
            with ui.row().classes("w-full gap-4 items-start"):
                slug_input = (
                    ui.input(label="Slug", placeholder="e.g. pubchem")
                    .props("outlined dense")
                    .classes("w-48")
                )
                name_input = (
                    ui.input(label="Display name", placeholder="e.g. PubChem")
                    .props("outlined dense")
                    .classes("w-64")
                )
                url_input = (
                    ui.input(label="URL", placeholder="https://mcp.example.org/mcp")
                    .props("outlined dense")
                    .classes("flex-grow")
                )
                register_button = ui.button("Register", icon="add").props("color=primary")
            form_errors = ui.column().classes("w-full")

        servers_container = ui.column().classes("w-full")

    async def load() -> None:
        servers_container.clear()
        access = await admin_access()
        if access is None:
            return
        try:
            async with access.session() as session:
                servers = await list_mcp_servers(session)
        except Exception:
            logger.exception("Error loading MCP servers")
            with servers_container:
                render_alert_banner(
                    title="Error",
                    message="Failed to load MCP servers. Check server logs.",
                    severity="error",
                )
            return
        with servers_container:
            if not servers:
                render_empty_state("No MCP servers registered yet.")
                return
            ui.table(columns=_COLUMNS, rows=_rows(servers), row_key="id").classes("w-full")

    def show_form_error(title: str, message: str) -> None:
        form_errors.clear()
        with form_errors:
            render_alert_banner(title=title, message=message, severity="error")

    async def save(registration: McpServerRegistration) -> bool | None:
        """Whether the registration was stored, or None when the caller is no
        longer an admin and nothing was read or written."""
        access = await admin_access()
        if access is None:
            return None
        try:
            async with access.session() as session:
                saved, message = await register_mcp_server(
                    session, registration, UUID(page_user_id)
                )
        except Exception:
            logger.exception("Failed to register MCP server %r", registration.slug)
            show_form_error("Error", "Failed to register the MCP server. Check server logs.")
            return False
        if not saved:
            show_form_error("Not registered", message)
            return False
        ui.notify(message, type="positive")
        return True

    @in_page_client
    async def register() -> None:
        if not register_button.enabled:
            return
        register_button.disable()
        try:
            form_errors.clear()
            if await admin_access() is None:
                servers_container.clear()
                return
            registration = parse_registration(slug_input.value, name_input.value, url_input.value)
            if isinstance(registration, list):
                show_form_error("Not registered", ". ".join(registration) + ".")
                return
            saved = await save(registration)
            if saved is None:
                servers_container.clear()
                return
            if saved:
                for field in (slug_input, name_input, url_input):
                    field.set_value("")
            await load()
        finally:
            register_button.enable()

    register_button.on_click(register)
    await load()

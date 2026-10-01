"""A2A connection details and administrator admission control."""

from uuid import UUID

from nicegui import ui

from openscientist.api.a2a import change_enabled, service_status
from openscientist.auth import get_current_user_id, is_current_user_admin, require_auth
from openscientist.settings import get_settings
from openscientist.webapp_components.ui_components import (
    render_a2a_quickstart,
    render_alert_banner,
    render_navigator,
)
from openscientist.webapp_components.utils import setup_timer_cleanup


@ui.page("/a2a-settings")
@require_auth
async def a2a_page() -> None:
    """Expose the same deployment setting to the authenticated web UI."""
    render_navigator(active_page="a2a")
    timers = setup_timer_cleanup()
    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-4"):
        ui.markdown("# Agent-to-agent access")
        with ui.card().classes("w-full gap-3"):
            ui.label("A2A · openscientist").classes("text-h6")
            status = ui.label("Checking A2A…")
            toggle = ui.switch("Enable A2A server", value=False)
            toggle.set_visibility(is_current_user_admin())
            toggle.disable()
            ui.label(
                "Clients submit ordinary discovery jobs using the agent and provider configured "
                "for this OpenScientist server. Jobs appear in the owner's normal job list."
            )
            card_url = ui.input("Agent card").props("readonly").classes("w-full")
            rpc_url = ui.input("Endpoint").props("readonly").classes("w-full")
            ui.label(
                "Turning off blocks discovery and further A2A requests. Accepted jobs continue; "
                "manage them in OpenScientist. The setting is saved across restarts."
            ).classes("text-sm text-grey-7")
            if not is_current_user_admin():
                ui.label("Only administrators can turn A2A on or off.").classes("text-sm")
            ui.link("Create or revoke an API key", "/api-keys")
            ui.label("Authenticate using Authorization: Bearer <name>:<secret>.").classes(
                "text-sm font-mono"
            )
            notices = ui.column().classes("w-full")

        render_a2a_quickstart(get_settings().auth.app_url)

        busy = False
        updating = False
        revision = 0

        async def refresh() -> None:
            nonlocal updating
            if busy:
                return
            observed_revision = revision
            try:
                data = await service_status()
                # A read started before a write must not overwrite its result.
                if busy or observed_revision != revision:
                    return
                updating = True
                toggle.set_value(data["enabled"])
                card_url.set_value(data["agent_card_url"])
                rpc_url.set_value(data["rpc_url"])
                status.set_text("A2A running" if data["enabled"] else "A2A off")
                toggle.enable()
                notices.clear()
            except Exception:
                status.set_text("A2A unavailable")
                toggle.disable()
                notices.clear()
                with notices:
                    render_alert_banner(
                        title="Could not confirm A2A status",
                        message="Check the server connection and database migrations. Retrying automatically.",
                        severity="error",
                    )
            finally:
                updating = False

        async def save() -> None:
            nonlocal busy, revision
            if updating or busy:
                return
            user_id = get_current_user_id()
            if not user_id:
                return
            busy = True
            revision += 1
            toggle.disable()
            try:
                await change_enabled(UUID(user_id), bool(toggle.value))
            except Exception:
                ui.notify(
                    "Could not change A2A settings. Administrator access is required.",
                    type="negative",
                )
            finally:
                busy = False
                await refresh()

        # Listen to browser changes, not programmatic set_value() refreshes.
        toggle.on("update:model-value", save)
        await refresh()
        timers.append(ui.timer(5, refresh))

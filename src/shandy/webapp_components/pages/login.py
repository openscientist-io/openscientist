"""Login page."""

import os

from nicegui import app, ui

from shandy.auth.oauth import is_mock_auth_enabled, is_oauth_configured
from shandy.webapp_components.utils.auth import check_password


@ui.page("/login")
def login_page():
    """Login page with OAuth support"""

    # Check if OAuth is configured
    oauth_enabled = is_oauth_configured()
    github_enabled = bool(os.getenv("GITHUB_CLIENT_ID"))
    orcid_enabled = bool(os.getenv("ORCID_CLIENT_ID"))
    mock_enabled = is_mock_auth_enabled()
    legacy_auth_enabled = bool(os.getenv("APP_PASSWORD_HASH"))

    # Check if already authenticated (from return redirect)
    if app.storage.user.get("authenticated"):
        return_to = app.storage.user.pop("return_to", "/")
        ui.navigate.to(return_to)
        return

    def try_login():
        if check_password(password_input.value):
            app.storage.user["authenticated"] = True
            return_to = app.storage.user.pop("return_to", "/")
            ui.navigate.to(return_to)
        else:
            ui.notify("Invalid password", color="negative")
            password_input.value = ""

    with ui.column().classes("absolute-center items-center"):
        ui.markdown("# SHANDY")
        ui.markdown("_Scientific Hypothesis Agent for Novel Discovery_")

        # OAuth login buttons
        if oauth_enabled:
            ui.label("Sign in with:").classes("text-lg mt-4 mb-2")

            if mock_enabled:
                ui.button(
                    "🧪 Mock Login (Dev)",
                    icon="bug_report",
                    on_click=lambda: ui.navigate.to("/auth/mock/login"),
                ).props("outline color=orange").classes("w-64")

            if github_enabled:
                ui.button(
                    "GitHub",
                    icon="github",
                    on_click=lambda: ui.navigate.to("/auth/github/login"),
                ).props("outline").classes("w-64 mt-2" if mock_enabled else "w-64")

            if orcid_enabled:
                ui.button(
                    "ORCID",
                    icon="badge",
                    on_click=lambda: ui.navigate.to("/auth/orcid/login"),
                ).props("outline").classes("w-64 mt-2")

        # Legacy password login (deprecated)
        if legacy_auth_enabled:
            if oauth_enabled:
                ui.separator().classes("w-64 my-4")
                ui.label("Or use legacy password:").classes("text-sm text-gray-600")

            password_input = (
                ui.input("Password", password=True, password_toggle_button=True)
                .classes("w-64 mt-2")
                .on("keydown.enter", try_login)
            )
            ui.button("Login", on_click=try_login).classes("w-64 mt-2")

        # No auth configured warning
        if not oauth_enabled and not legacy_auth_enabled:
            ui.markdown(
                "⚠️ **No authentication configured**\n\n"
                "Please configure OAuth providers or set APP_PASSWORD_HASH in .env"
            ).classes("text-center mt-4")

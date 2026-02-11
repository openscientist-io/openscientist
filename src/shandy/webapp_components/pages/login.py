"""Login page with OAuth authentication."""

import os

from nicegui import app, ui

from shandy.auth.oauth import is_mock_auth_enabled, is_oauth_configured


@ui.page("/login")
def login_page():
    """Login page with OAuth support."""

    # Check if OAuth is configured
    oauth_enabled = is_oauth_configured()
    github_enabled = bool(os.getenv("GITHUB_CLIENT_ID"))
    orcid_enabled = bool(os.getenv("ORCID_CLIENT_ID"))
    mock_enabled = is_mock_auth_enabled()

    # Check if already authenticated (from return redirect)
    if app.storage.user.get("authenticated"):
        return_to = app.storage.user.pop("return_to", "/")
        ui.navigate.to(return_to)
        return

    with ui.column().classes("absolute-center items-center gap-4"):
        ui.markdown("# SHANDY")
        ui.markdown("_Scientific Hypothesis Agent for Novel Discovery_")

        ui.space()

        # OAuth buttons container
        with ui.column().classes("gap-3 w-72"):
            if oauth_enabled:
                # GitHub OAuth button
                if github_enabled:
                    with (
                        ui.button(on_click=lambda: ui.navigate.to("/auth/github/login"))
                        .classes("w-full justify-start")
                        .style("background-color: #24292e; color: white;")
                    ):
                        with ui.row().classes("items-center gap-3"):
                            ui.html(
                                '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">'
                                '<path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>'
                                "</svg>"
                            )
                            ui.label("Continue with GitHub")

                # ORCID OAuth button
                if orcid_enabled:
                    with (
                        ui.button(on_click=lambda: ui.navigate.to("/auth/orcid/login"))
                        .classes("w-full justify-start")
                        .style("background-color: #a6ce39; color: white;")
                    ):
                        with ui.row().classes("items-center gap-3"):
                            ui.html(
                                '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">'
                                '<path d="M12 0C5.372 0 0 5.372 0 12s5.372 12 12 12 12-5.372 12-12S18.628 0 12 0zM7.369 4.378c.525 0 .947.431.947.947s-.422.947-.947.947a.95.95 0 01-.947-.947c0-.525.422-.947.947-.947zm-.722 3.038h1.444v10.041H6.647V7.416zm3.562 0h3.9c3.712 0 5.344 2.653 5.344 5.025 0 2.578-2.016 5.025-5.325 5.025h-3.919V7.416zm1.444 1.303v7.444h2.297c3.272 0 4.022-2.484 4.022-3.722 0-1.209-.619-3.722-3.853-3.722h-2.466z"/>'
                                "</svg>"
                            )
                            ui.label("Continue with ORCID")

                # Mock OAuth button (dev mode only)
                if mock_enabled:
                    ui.separator().classes("my-2")
                    ui.label("Development Mode").classes("text-xs text-gray-500")
                    with (
                        ui.button(on_click=lambda: ui.navigate.to("/auth/mock/login"))
                        .classes("w-full justify-start")
                        .props("color=orange")
                    ):
                        with ui.row().classes("items-center gap-3"):
                            ui.icon("developer_mode", size="20px")
                            ui.label("Mock Login (Dev Only)")

            # No auth configured warning
            if not oauth_enabled:
                with ui.card().classes(
                    "w-full bg-yellow-50 border-l-4 border-yellow-500"
                ):
                    with ui.row().classes("items-center gap-3"):
                        ui.icon("warning", color="orange", size="md")
                        with ui.column().classes("gap-1"):
                            ui.label("No Authentication Configured").classes(
                                "text-yellow-800 font-bold"
                            )
                            ui.label(
                                "Please configure OAuth providers in .env"
                            ).classes("text-yellow-700 text-sm")

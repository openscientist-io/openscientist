"""Login page with OAuth authentication."""

from nicegui import app, ui

from shandy.auth.oauth import is_mock_auth_enabled, is_oauth_configured
from shandy.settings import get_settings
from shandy.webapp_components.ui_components import SHANDY_THINKING_SVG


@ui.page("/login")
def login_page():
    """Login page with OAuth support."""

    # Check if OAuth is configured
    settings = get_settings()
    oauth_enabled = is_oauth_configured()
    github_enabled = bool(settings.auth.github_client_id)
    orcid_enabled = bool(settings.auth.orcid_client_id)
    mock_enabled = is_mock_auth_enabled()

    # Check if already authenticated (from return redirect)
    if app.storage.user.get("authenticated"):
        return_to = app.storage.user.pop("return_to", "/")
        ui.navigate.to(return_to)
        return

    # Page styling - cyan gradient background
    ui.add_head_html(
        """
        <style>
            body {
                background: linear-gradient(135deg, #ecfeff 0%, #cffafe 50%, #a5f3fc 100%);
                min-height: 100vh;
            }
            .login-card {
                background: white;
                border-radius: 16px;
                box-shadow: 0 10px 40px rgba(14, 116, 144, 0.15);
                border: 1px solid #a5f3fc;
            }
            .login-btn {
                transition: transform 0.2s ease, box-shadow 0.2s ease;
            }
            .login-btn:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
            }
        </style>
    """
    )

    with ui.column().classes("absolute-center items-center"):
        # Login card
        with ui.card().classes("login-card p-8 items-center gap-6"):
            # Animated SHANDY logo
            ui.html(SHANDY_THINKING_SVG).classes("w-16 h-16")

            # Title and subtitle
            ui.label("SHANDY").classes("text-3xl font-bold text-cyan-800")
            ui.label("Scientific Hypothesis Agent for Novel Discovery").classes(
                "text-sm text-cyan-600 text-center max-w-xs"
            )

            ui.separator().classes("w-full my-2")

            # OAuth buttons container - always show all providers
            with ui.column().classes("gap-3 w-72"):
                # Google OAuth button
                google_enabled = bool(settings.auth.google_client_id)
                with (
                    ui.button(
                        on_click=lambda: (
                            ui.navigate.to("/auth/google/login") if google_enabled else None
                        )
                    )
                    .classes("w-full login-btn")
                    .style(
                        "background-color: #4285f4; color: white; "
                        "justify-content: flex-start; padding-left: 12px;"
                    )
                    .props("disable" if not google_enabled else "")
                ):
                    with ui.row().classes("items-center w-full"):
                        ui.html(
                            '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">'
                            '<path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>'
                            '<path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>'
                            '<path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>'
                            '<path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>'
                            "</svg>"
                        )
                        ui.element("div").classes("w-px h-5 bg-white/40 mx-3")
                        ui.label("Continue with Google")

                # GitHub OAuth button
                with (
                    ui.button(
                        on_click=lambda: (
                            ui.navigate.to("/auth/github/login") if github_enabled else None
                        )
                    )
                    .classes("w-full login-btn")
                    .style(
                        "background-color: #24292e; color: white; "
                        "justify-content: flex-start; padding-left: 12px;"
                    )
                    .props("disable" if not github_enabled else "")
                ):
                    with ui.row().classes("items-center w-full"):
                        ui.html(
                            '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">'
                            '<path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>'
                            "</svg>"
                        )
                        ui.element("div").classes("w-px h-5 bg-white/40 mx-3")
                        ui.label("Continue with GitHub")

                # ORCID OAuth button
                with (
                    ui.button(
                        on_click=lambda: (
                            ui.navigate.to("/auth/orcid/login") if orcid_enabled else None
                        )
                    )
                    .classes("w-full login-btn")
                    .style(
                        "background-color: #a6ce39; color: white; "
                        "justify-content: flex-start; padding-left: 12px;"
                    )
                    .props("disable" if not orcid_enabled else "")
                ):
                    with ui.row().classes("items-center w-full"):
                        ui.html(
                            '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">'
                            '<path d="M12 0C5.372 0 0 5.372 0 12s5.372 12 12 12 12-5.372 12-12S18.628 0 12 0zM7.369 4.378c.525 0 .947.431.947.947s-.422.947-.947.947a.95.95 0 01-.947-.947c0-.525.422-.947.947-.947zm-.722 3.038h1.444v10.041H6.647V7.416zm3.562 0h3.9c3.712 0 5.344 2.653 5.344 5.025 0 2.578-2.016 5.025-5.325 5.025h-3.919V7.416zm1.444 1.303v7.444h2.297c3.272 0 4.022-2.484 4.022-3.722 0-1.209-.619-3.722-3.853-3.722h-2.466z"/>'
                            "</svg>"
                        )
                        ui.element("div").classes("w-px h-5 bg-white/40 mx-3")
                        ui.label("Continue with ORCID")

                # Mock OAuth buttons (dev mode only)
                if mock_enabled:
                    ui.separator().classes("my-2")
                    ui.label("Development Mode").classes("text-xs text-gray-500 text-center w-full")
                    with (
                        ui.button(on_click=lambda: ui.navigate.to("/auth/mock/login"))
                        .classes("w-full login-btn")
                        .style("justify-content: flex-start; padding-left: 12px;")
                        .props("color=orange")
                    ):
                        with ui.row().classes("items-center w-full"):
                            ui.icon("developer_mode", size="20px")
                            ui.element("div").classes("w-px h-5 bg-white/40 mx-3")
                            ui.label("Mock Login (Dev Only)")

                    with (
                        ui.button(on_click=lambda: ui.navigate.to("/auth/mock/admin-login"))
                        .classes("w-full login-btn")
                        .style("justify-content: flex-start; padding-left: 12px;")
                        .props("color=red")
                    ):
                        with ui.row().classes("items-center w-full"):
                            ui.icon("admin_panel_settings", size="20px")
                            ui.element("div").classes("w-px h-5 bg-white/40 mx-3")
                            ui.label("Mock Admin Login")

                # Warning if no auth is configured
                if not oauth_enabled and not mock_enabled:
                    with ui.card().classes("w-full bg-yellow-50 border-l-4 border-yellow-500"):
                        with ui.row().classes("items-center gap-3"):
                            ui.icon("warning", color="orange", size="md")
                            with ui.column().classes("gap-1"):
                                ui.label("No Authentication Configured").classes(
                                    "text-yellow-800 font-bold"
                                )
                                ui.label("Please configure OAuth providers in .env").classes(
                                    "text-yellow-700 text-sm"
                                )

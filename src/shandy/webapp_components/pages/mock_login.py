"""Mock login form page for development testing."""

import os

from nicegui import ui


@ui.page("/mock-login-form")
def mock_login_form():
    """Mock OAuth login form for development testing."""

    # Security check - only show in development mode
    if not os.getenv("ENABLE_MOCK_AUTH"):
        ui.label("Mock authentication is not enabled").classes("text-center mt-8")
        ui.button("Back to Login", on_click=lambda: ui.navigate.to("/login")).classes("mt-4")
        return

    with ui.column().classes("absolute-center items-center"):
        ui.markdown("# 🧪 Mock OAuth Login")
        ui.markdown("_Development Testing Only_").classes("text-sm text-gray-600 mb-4")

        ui.label("Enter test user information:").classes("text-lg mb-2")

        # Create form
        with ui.card().classes("w-96 p-4"):
            email_input = ui.input(
                "Email", placeholder="dev@example.com", value="dev@example.com"
            ).classes("w-full")

            name_input = ui.input("Name", placeholder="Dev User", value="Dev User").classes(
                "w-full mt-2"
            )

            username_input = ui.input("Username", placeholder="devuser", value="devuser").classes(
                "w-full mt-2"
            )

            ui.markdown(
                "**Note:** This creates a test user without real authentication. "
                "Never enable this in production!"
            ).classes("text-xs text-orange-600 mt-4")

            async def submit_mock_login():
                """Submit mock login form."""
                # Create form data
                import httpx

                form_data = {
                    "email": email_input.value,
                    "name": name_input.value,
                    "username": username_input.value,
                }

                # Submit to mock callback endpoint
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{os.getenv('APP_URL', 'http://localhost:8080')}/auth/mock/callback",
                        data=form_data,
                        follow_redirects=False,
                    )

                    if response.status_code in (303, 302):
                        # Get the session cookie from response
                        if "set-cookie" in response.headers:
                            # Navigate to home page
                            ui.navigate.to("/")
                        else:
                            ui.notify("Login failed - no session cookie", color="negative")
                    else:
                        ui.notify("Login failed", color="negative")

            # Buttons
            with ui.row().classes("w-full justify-between mt-4"):
                ui.button("Cancel", on_click=lambda: ui.navigate.to("/login")).props("flat")

                ui.button("Sign In", on_click=submit_mock_login).props("color=primary")

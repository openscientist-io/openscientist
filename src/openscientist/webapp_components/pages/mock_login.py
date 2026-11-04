"""Mock login form page for development testing."""

import uuid

from nicegui import ui

from openscientist.settings import get_settings

_FORM_FIELD_STYLE = (
    "width: 100%; padding: 8px; margin-top: 4px; "
    "border: 1px solid #a5f3fc; border-radius: 8px; box-sizing: border-box;"
)


def _login_form_html(email: str, name: str, username: str) -> str:
    """A plain HTML form the browser posts to the mock callback itself, so
    the session cookie lands in the visitor's jar."""
    return f"""
        <form method="post" action="/auth/mock/callback" style="display: grid; gap: 12px;">
            <label>
                Email
                <input type="text" name="email" value="{email}" required style="{_FORM_FIELD_STYLE}">
            </label>
            <label>
                Name
                <input type="text" name="name" value="{name}" required style="{_FORM_FIELD_STYLE}">
            </label>
            <label>
                Username
                <input type="text" name="username" value="{username}" required style="{_FORM_FIELD_STYLE}">
            </label>
            <button type="submit"
                    style="padding: 8px 16px; background: #0891b2; color: white;
                           border: none; border-radius: 8px; cursor: pointer;">
                Sign In
            </button>
        </form>
    """


@ui.page("/mock-login-form")
def mock_login_form() -> None:
    """Mock OAuth login form for development testing."""

    # Security check - only show in development mode
    settings = get_settings()
    if not settings.dev.dev_mode:
        ui.label("Mock authentication is not enabled").classes("text-center mt-8")
        ui.button("Back to Login", on_click=lambda: ui.navigate.to("/login")).classes("mt-4")
        return

    # Generate random default values for convenience
    random_id = uuid.uuid4().hex[:8]
    default_email = f"dev-{random_id}@example.com"
    default_name = f"Dev User {random_id}"
    default_username = f"devuser{random_id}"

    with ui.column().classes("absolute-center items-center"):
        ui.markdown("# 🧪 Mock OAuth Login")
        ui.markdown("_Development Testing Only_").classes("text-sm text-gray-600 mb-4")

        ui.label("Enter test user information:").classes("text-lg mb-2")

        # Create form with random defaults
        with ui.card().classes("w-96 p-4"):
            ui.html(
                _login_form_html(default_email, default_name, default_username),
                sanitize=False,
            )

            ui.markdown(
                "**Note:** This creates a test user without real authentication. "
                "Never enable this in production!"
            ).classes("text-xs text-orange-600 mt-4")

            ui.button("Cancel", on_click=lambda: ui.navigate.to("/login")).props("flat w-full")

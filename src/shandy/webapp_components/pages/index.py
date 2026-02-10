"""Homepage/index page - redirects to jobs list."""

from nicegui import ui

from shandy.auth import require_auth


@ui.page("/")
@require_auth
def index_page():
    """Homepage - redirects to jobs list."""
    # Redirect to jobs page
    ui.navigate.to("/jobs")

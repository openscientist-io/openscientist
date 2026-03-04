"""Homepage/index page - redirects to jobs list."""

from nicegui import ui

from open_scientist.auth import require_auth


@ui.page("/")
@require_auth
async def index_page() -> None:
    """Homepage - redirects to jobs list.

    This is async to ensure the session cookie is validated on first visit
    and the authenticated flag is set in app.storage.user before redirecting
    to other (sync) pages.
    """
    # Redirect to jobs page
    ui.navigate.to("/jobs")

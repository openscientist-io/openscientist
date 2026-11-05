"""Homepage/index page - redirects to jobs list."""

from nicegui import ui
from starlette.responses import RedirectResponse

from openscientist.auth import require_auth
from openscientist.auth.middleware import HOME_PATH, redirect_to


@ui.page("/")
@require_auth
async def index_page() -> RedirectResponse:
    """Send a signed-in visitor to the jobs list, as a server redirect.

    Behind ``require_auth`` so a signed-out visitor goes to the login page
    and comes back here after signing in.
    """
    return redirect_to(HOME_PATH)

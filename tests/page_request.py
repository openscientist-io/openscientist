"""The request NiceGUI binds to a page's build and to every event on it."""

from contextvars import ContextVar
from types import SimpleNamespace


def page_request_storage(request: object) -> SimpleNamespace:
    """A stand-in for ``nicegui.storage`` whose bound page request is ``request``,
    to patch over ``openscientist.auth.middleware.storage``."""
    return SimpleNamespace(request_contextvar=ContextVar("page_request", default=request))

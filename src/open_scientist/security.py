"""
Security middleware for OpenScientist.

Blocks automated scanner probes before they reach NiceGUI routing,
reducing log noise and unnecessary database sessions.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Path prefixes that no legitimate client should request on this Python app.
# These are probes from automated vulnerability scanners.
_BLOCKED_PREFIXES: tuple[str, ...] = (
    "/.git/",
    "/.env",  # .env, .env.local, .env.production, etc.
    "/.aws/",
    "/.ssh/",
    "/wp-",  # /wp-admin/, /wp-content/, /wp-login.php, /wp-includes/, etc.
    "/wordpress/",
    "/xmlrpc.php",
    "/phpmyadmin",
    "/.htaccess",
    "/.DS_Store",
    # DNS-over-HTTPS relay probes (RFC 8484 and variants)
    "/dns-query",
    "/resolve",
    "/query",
)

# Path suffixes that indicate scanner probes on this Python app.
_BLOCKED_SUFFIXES: tuple[str, ...] = (
    "/wlwmanifest.xml",
    "/xmlrpc.php",
    ".php",
    ".asp",
    ".aspx",
    ".jsp",
)


class ScannerBlockMiddleware(BaseHTTPMiddleware):
    """
    Silently reject well-known scanner probe paths with a 404.

    Returns a bare response before NiceGUI routing runs, avoiding
    unnecessary DB sessions and WARNING-level log noise.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path.startswith(_BLOCKED_PREFIXES) or any(path.endswith(s) for s in _BLOCKED_SUFFIXES):
            logger.debug("Blocked scanner probe: %s %s", request.method, path)
            return Response(status_code=404)
        return await call_next(request)


def register_scanner_block_middleware(app: Any) -> bool:
    """Register scanner middleware if possible.

    Returns:
        True if middleware is present (already registered or newly added).
    """
    existing_middleware = getattr(app, "user_middleware", [])
    if any(
        getattr(middleware, "cls", None) is ScannerBlockMiddleware
        for middleware in existing_middleware
    ):
        return True

    app.add_middleware(ScannerBlockMiddleware)
    return True

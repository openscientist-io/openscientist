"""
Security middleware for OpenScientist.

Blocks automated scanner probes before they reach NiceGUI routing, and sets
the deployment's security response headers (CSP, HSTS, framing, referrer).
"""

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
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


def register_scanner_block_middleware(app: FastAPI) -> bool:
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


# App-page CSP. nicegui.js evaluates the server-sent element code and Vue
# compiles element templates at runtime, so 'unsafe-inline'/'unsafe-eval'
# are required; everything else stays same-origin so the page never loads
# or connects to a third-party host.
_APP_CSP = (
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "frame-src 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_HSTS = "max-age=31536000; includeSubDomains"
_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(), payment=(), usb=()"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set the deployment's security headers on every response.

    A route that already sets its own ``Content-Security-Policy`` (the
    sandboxed agent-content routes) keeps it, and is left frameable; the
    app policy and ``X-Frame-Options`` are only layered over responses
    that carry no CSP. ``Strict-Transport-Security`` is only sent when the
    deployment's ``APP_URL`` is https.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        headers = response.headers

        if "content-security-policy" not in headers:
            headers["content-security-policy"] = _APP_CSP
            headers["x-frame-options"] = "DENY"
        if "referrer-policy" not in headers:
            headers["referrer-policy"] = "strict-origin-when-cross-origin"
        if "x-content-type-options" not in headers:
            headers["x-content-type-options"] = "nosniff"
        if "permissions-policy" not in headers:
            headers["permissions-policy"] = _PERMISSIONS_POLICY
        if _serves_https() and "strict-transport-security" not in headers:
            headers["strict-transport-security"] = _HSTS

        return response


def _serves_https() -> bool:
    from openscientist.settings import get_settings

    try:
        return get_settings().auth.app_url.startswith("https")
    except Exception:
        # Broken settings are reported by the config-error page, not by 500ing every response.
        return False


def register_security_headers_middleware(app: FastAPI) -> bool:
    """Register the security-headers middleware if possible.

    Returns:
        True if middleware is present (already registered or newly added).
    """
    existing_middleware = getattr(app, "user_middleware", [])
    if any(
        getattr(middleware, "cls", None) is SecurityHeadersMiddleware
        for middleware in existing_middleware
    ):
        return True

    app.add_middleware(SecurityHeadersMiddleware)
    return True

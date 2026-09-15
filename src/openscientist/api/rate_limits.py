"""SlowAPI rate limiting configuration and shared limit strings."""

from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

HEALTH_RATE_LIMIT = "10/minute"
AUTH_RATE_LIMIT = "10/minute"
MUTATING_RATE_LIMIT = "30/minute"

limiter = Limiter(key_func=get_remote_address)


def wire_rate_limiter(app: FastAPI) -> None:
    """Attach the shared limiter instance to an application's state."""
    app.state.limiter = limiter


def configure_host_rate_limiting(host_app: FastAPI) -> None:
    """Enable SlowAPI on the host FastAPI application."""
    wire_rate_limiter(host_app)
    host_app.add_exception_handler(
        RateLimitExceeded,
        _rate_limit_exceeded_handler,  # type: ignore[arg-type]
    )
    host_app.add_middleware(SlowAPIMiddleware)

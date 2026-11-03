"""HTTP answers for database errors raised while serving a request."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from openscientist.database.retry import DatabaseUnavailableError, is_connection_error

logger = logging.getLogger(__name__)


async def _database_error(request: Request, exc: Exception) -> JSONResponse:
    if not is_connection_error(exc):
        raise exc
    logger.error("Database unavailable for %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database unavailable"},
    )


def register_database_error_handlers(app: FastAPI) -> None:
    """Answer 503 when the database is unreachable. Other database errors stay 500."""
    app.add_exception_handler(DatabaseUnavailableError, _database_error)
    app.add_exception_handler(DBAPIError, _database_error)

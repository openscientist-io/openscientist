"""Main API router for the OpenScientist REST API."""

import logging

from fastapi import APIRouter, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from .endpoints import jobs_router, keys_router, shares_router, skills_router

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(jobs_router)
api_router.include_router(keys_router)
api_router.include_router(shares_router)
api_router.include_router(skills_router)


@api_router.get("/health", tags=["Health"])
@limiter.limit("10/minute")
async def health_check(request: Request) -> dict[str, str]:
    """Return the API version and status."""
    _ = request
    return {
        "status": "ok",
        "version": "v1",
        "api": "openscientist",
    }

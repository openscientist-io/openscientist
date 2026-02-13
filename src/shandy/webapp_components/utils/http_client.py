"""HTTP client utilities with session cookie authentication."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from nicegui import app


@asynccontextmanager
async def authenticated_client(
    base_url: str = "http://localhost:8080",
) -> AsyncIterator[httpx.AsyncClient]:
    """
    Create httpx client with session cookies pre-configured.

    Args:
        base_url: Base URL for the API (default: http://localhost:8080)

    Yields:
        AsyncClient with cookies set from app.storage.browser
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        cookies=dict(app.storage.browser),
    ) as client:
        yield client


async def api_get(path: str) -> httpx.Response:
    """
    Make authenticated GET request.

    Args:
        path: API path (e.g., "/web/shares/job/123")

    Returns:
        httpx.Response
    """
    async with authenticated_client() as client:
        return await client.get(path)


async def api_post(path: str, json: dict | None = None) -> httpx.Response:
    """
    Make authenticated POST request.

    Args:
        path: API path
        json: Optional JSON body

    Returns:
        httpx.Response
    """
    async with authenticated_client() as client:
        return await client.post(path, json=json)


async def api_delete(path: str) -> httpx.Response:
    """
    Make authenticated DELETE request.

    Args:
        path: API path

    Returns:
        httpx.Response
    """
    async with authenticated_client() as client:
        return await client.delete(path)

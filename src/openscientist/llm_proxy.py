"""Web-side LLM key-replacement proxy for the untrusted job container.

Authenticates a per-job placeholder, swaps in the real provider credential, and
streams the response. Listens on a fixed internal port, unpublished to the host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from openscientist.job_container.secrets import verify_job_placeholder
from openscientist.providers import get_provider
from openscientist.providers.base import AirgapEgress, LlmUpstream
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

LLM_PROXY_PORT = 8081
_WEB_HOST_ENV = "OPENSCIENTIST_WEB_HOST"
_DEFAULT_WEB_HOST = "openscientist"

# Dropped when forwarding: connection-scoped headers and the auth header we replace.
_DROP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
        "te",
        "trailer",
        "x-api-key",
        "authorization",
    }
)
_DROP_RESPONSE_HEADERS = frozenset(
    {"content-length", "connection", "keep-alive", "transfer-encoding", "upgrade", "trailer"}
)


def container_proxy_base_url() -> str:
    """Proxy base URL as reached from a sibling job container on the compose network."""
    host = os.environ.get(_WEB_HOST_ENV, _DEFAULT_WEB_HOST)
    return f"http://{host}:{LLM_PROXY_PORT}"


def _presented_credential(request: Request) -> str | None:
    """The placeholder the agent sent, from whichever auth header it used."""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:]
    return None


def _apply_request_overrides(body: bytes, overrides: dict[str, Any]) -> bytes:
    """Force an upstream's required request fields into a JSON body.

    These take precedence over what the caller sent. They exist for hard
    upstream incompatibilities -- a field the agent may set but this upstream
    cannot honour without producing a reply the agent then fails to parse -- so
    deferring to the caller would just reinstate the bug intermittently,
    whenever the CLI happened to send the field. A body that is not a JSON
    object is passed through untouched: the proxy forwards every path, not just
    the Messages API.
    """
    if not overrides or not body:
        return body
    try:
        payload = json.loads(body)
    except ValueError:
        return body
    if not isinstance(payload, dict):
        return body
    forced = sorted(k for k in overrides if k in payload and payload[k] != overrides[k])
    if forced:
        logger.info("LLM proxy: forcing upstream-required field(s) over caller values: %s", forced)
    merged = {**payload, **overrides}
    if merged == payload:
        return body
    return json.dumps(merged).encode()


def create_llm_proxy_app(
    *,
    master_key: Callable[[], str],
    upstream: Callable[[], LlmUpstream],
    client: httpx.AsyncClient,
) -> Starlette:
    """Build the proxy ASGI app. The callables are resolved per request."""

    async def handler(request: Request) -> Response:
        credential = _presented_credential(request)
        if credential is None or not verify_job_placeholder(master_key(), credential):
            return Response("unauthorized", status_code=401)
        try:
            target = upstream()
        except ValueError:
            logger.warning("LLM proxy: active provider is not supported")
            return Response("provider not supported", status_code=502)

        body = _apply_request_overrides(await request.body(), target.request_overrides)
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS
        }
        headers.update(target.auth_headers)
        url = target.base_url + request.url.path
        if request.url.query:
            url = f"{url}?{request.url.query}"

        upstream_req = client.build_request(request.method, url, headers=headers, content=body)
        upstream_resp = await client.send(upstream_req, stream=True)
        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in _DROP_RESPONSE_HEADERS
        }
        return StreamingResponse(
            upstream_resp.aiter_raw(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            background=BackgroundTask(upstream_resp.aclose),
        )

    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                handler,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ]
    )


class _NoSignalServer(uvicorn.Server):
    """A second server sharing the loop must not install signal handlers."""

    def install_signal_handlers(self) -> None:
        return None


_proxy_server: uvicorn.Server | None = None
_proxy_task: asyncio.Task[None] | None = None


def _active_upstream() -> LlmUpstream:
    upstream = get_provider().llm_upstream()
    if upstream is None:
        raise ValueError("active provider is not proxied")
    return upstream


async def start_llm_proxy() -> None:
    """Start the proxy listener as a loop task when the provider is proxied."""
    global _proxy_server, _proxy_task
    if _proxy_task is not None:
        return
    if get_provider().airgap_egress().mode is not AirgapEgress.PROXY:
        logger.info(
            "LLM proxy: provider %r not covered, not starting",
            get_settings().provider.provider_id,
        )
        return

    client = httpx.AsyncClient(timeout=httpx.Timeout(None))
    app = create_llm_proxy_app(
        master_key=lambda: get_settings().secret_key,
        upstream=_active_upstream,
        client=client,
    )
    config = uvicorn.Config(app, host="0.0.0.0", port=LLM_PROXY_PORT, log_level="warning")
    _proxy_server = _NoSignalServer(config)
    _proxy_task = asyncio.create_task(_proxy_server.serve())
    logger.info("LLM key-replacement proxy listening on port %d", LLM_PROXY_PORT)

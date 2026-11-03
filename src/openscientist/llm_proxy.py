"""Web-side LLM key-replacement proxy for the untrusted job container.

Authenticates a per-job placeholder, swaps in the real provider credential, and
streams the response. Listens on an internal port, unpublished to the host.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from openscientist.job_container.secrets import proxy_run
from openscientist.listener import bind_listener
from openscientist.providers import get_provider
from openscientist.providers.base import LlmUpstream
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

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
    return f"http://{host}:{get_settings().container.llm_proxy_port}"


def _presented_credential(request: Request) -> str | None:
    """The placeholder the agent sent, from whichever auth header it used."""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:]
    return None


def create_llm_proxy_app(
    *,
    upstream: Callable[[], LlmUpstream],
    client: httpx.AsyncClient,
) -> Starlette:
    """Build the proxy ASGI app. The callables are resolved per request, and only
    a live run's credential is accepted."""

    async def handler(request: Request) -> Response:
        credential = _presented_credential(request)
        # The run, not just the fact that it is live: attribution rides the
        # forwarded request's log line.
        run = proxy_run(credential) if credential else None
        if run is None:
            return Response("unauthorized", status_code=401)
        try:
            target = upstream()
        except ValueError:
            logger.warning("LLM proxy: active provider is not supported")
            return Response("provider not supported", status_code=502)

        body = await request.body()
        logger.debug(
            "LLM proxy: %s %s for run %s (%s)",
            request.method,
            request.url.path,
            run.job_id,
            run.kind,
        )
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS
        }
        headers.update(
            target.signing.credential_headers(
                method=request.method, path=request.url.path, query=request.url.query, body=body
            )
        )
        url = target.signing.upstream_url(
            base_url=target.base_url, path=request.url.path, query=request.url.query
        )

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
    """Start the proxy listener as a loop task, for every provider. A provider
    the active signing cannot serve (no upstream) still gets the listener, so
    its container's traffic is refused by the proxy rather than reaching the
    vendor with a real credential.

    Raises:
        ListenerBindError: Its port cannot be bound.
    """
    global _proxy_server, _proxy_task
    if _proxy_task is not None:
        return

    port = get_settings().container.llm_proxy_port
    sock = bind_listener(port, name="LLM proxy", setting="OPENSCIENTIST_LLM_PROXY_PORT")
    client = httpx.AsyncClient(timeout=httpx.Timeout(None))
    app = create_llm_proxy_app(upstream=_active_upstream, client=client)
    config = uvicorn.Config(app, log_level="warning")
    _proxy_server = _NoSignalServer(config)
    _proxy_task = asyncio.create_task(_proxy_server.serve(sockets=[sock]))
    logger.info("LLM key-replacement proxy listening on port %d", port)

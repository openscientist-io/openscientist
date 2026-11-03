"""Web-side LLM key-replacement proxy for the untrusted job container.

Authenticates a per-job placeholder, swaps in the real provider credential, and
streams the response. Listens on an internal port, unpublished to the host.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import suppress

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from openscientist.job_container.secrets import proxy_run
from openscientist.listener import bind_listener
from openscientist.providers import get_provider
from openscientist.providers.base import LlmUpstream, UpstreamCredentialError
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


def _presented_credential_from_headers(headers: Mapping[str, str]) -> str | None:
    """The placeholder presented by a request, from whichever auth header was
    used."""
    api_key = headers.get("x-api-key")
    if api_key:
        return api_key
    auth = headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:]
    return None


def _presented_credential(request: Request) -> str | None:
    """The placeholder the agent sent, from whichever auth header it used."""
    return _presented_credential_from_headers(request.headers)


def _asgi_header_map(pairs: list[tuple[bytes, bytes] | tuple[str, str]]) -> dict[str, str]:
    """ASGI header pairs (bytes or str, per the uvicorn implementation) as a
    lowercase-keyed str map."""
    return {
        key.decode("latin-1").lower() if isinstance(key, bytes) else key.lower(): (
            value.decode("latin-1") if isinstance(value, bytes) else value
        )
        for key, value in pairs
    }


# Dropped from a websocket handshake when forwarding: connection-scoped and
# handshake-only headers, plus the auth headers the proxy re-signs.
_WS_DROP_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "proxy-authorization",
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
    }
)

# RFC 6455 codes reserved for local use, which a close frame must not carry.
_LOCAL_ONLY_CLOSE_CODES = {1005, 1006, 1015}


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
        try:
            headers.update(
                target.signing.credential_headers(
                    method=request.method,
                    path=request.url.path,
                    query=request.url.query,
                    body=body,
                )
            )
        except UpstreamCredentialError:
            logger.warning("LLM proxy: upstream credential unusable for run %s", run.job_id)
            return Response("upstream credential unavailable", status_code=401)
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

    terminator = _ResponsesWebsocketTerminator(upstream=upstream, client=client)
    return Starlette(
        routes=[
            WebSocketRoute("/responses", terminator),
            Route(
                "/{path:path}",
                handler,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            ),
        ]
    )


async def _refuse_websocket(send, status_code: int, detail: str) -> None:
    """End a websocket handshake with an HTTP status, so a dead run is
    refused the way the REST path refuses it, before any upstream is
    touched."""
    body = detail.encode()
    await send(
        {
            "type": "websocket.http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "websocket.http.response.body", "body": body, "more_body": False})


class _ResponsesWebsocketTerminator:
    """Terminates the codex Responses websocket on the web side.

    Authenticates the run's placeholder, opens the upstream socket with the
    web-side credential, and pumps frames unchanged in both directions with
    close codes propagated. Raw ASGI, so a dead run or an unusable credential
    is refused with an HTTP status before the upstream is touched.
    """

    def __init__(self, *, upstream: Callable[[], LlmUpstream], client: httpx.AsyncClient) -> None:
        self._upstream = upstream
        self._client = client

    async def __call__(self, scope, receive, send) -> None:
        connect_msg = await receive()
        if connect_msg.get("type") != "websocket.connect":
            return
        # The ASGI spec carries the handshake headers on the scope, not the
        # connect message; fall back to the message for implementations that
        # do add them there.
        raw = connect_msg.get("headers") or scope.get("headers", [])
        headers = _asgi_header_map(raw)
        credential = _presented_credential_from_headers(headers)
        # The run, not just the fact that it is live: attribution rides the
        # forwarded request's log line.
        run = proxy_run(credential) if credential else None
        if run is None:
            await _refuse_websocket(send, 401, "unauthorized")
            return
        try:
            target = self._upstream()
        except ValueError:
            logger.warning("LLM proxy: active provider is not supported")
            await _refuse_websocket(send, 502, "provider not supported")
            return
        if target is None:
            await _refuse_websocket(send, 502, "provider not proxied")
            return
        query = scope.get("query_string", b"").decode()
        try:
            signing_headers = target.signing.credential_headers(
                method="GET", path=scope["path"], query=query, body=b""
            )
        except UpstreamCredentialError:
            logger.warning("LLM proxy: upstream credential unusable for run %s", run.job_id)
            await _refuse_websocket(send, 401, "upstream credential unavailable")
            return
        logger.debug("LLM proxy: websocket %s for run %s (%s)", scope["path"], run.job_id, run.kind)
        url = target.signing.upstream_websocket_url(
            base_url=target.base_url, path=scope["path"], query=query
        )
        forwarded = {k: v for k, v in headers.items() if k not in _WS_DROP_HEADERS}
        forwarded.update(signing_headers)
        try:
            connection = await ws_connect(
                url, additional_headers=forwarded, proxy=None, max_size=2**24
            )
        except Exception as exc:
            logger.warning("LLM proxy: upstream websocket unreachable (%s)", exc)
            await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "websocket.accept"})
        loop = asyncio.get_running_loop()
        to_upstream = loop.create_task(self._container_to_upstream(connection, receive))
        to_container = loop.create_task(self._upstream_to_container(connection, send))
        await asyncio.wait({to_upstream, to_container}, return_when=asyncio.FIRST_COMPLETED)
        for task in (to_upstream, to_container):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        with suppress(Exception):
            await connection.close()

    async def _container_to_upstream(self, connection, receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                code = message.get("code")
                reason = message.get("reason") or ""
                if code is None or code in _LOCAL_ONLY_CLOSE_CODES:
                    code, reason = 1011, ""
                await connection.close(code, reason)
                return
            if message.get("text") is not None:
                await connection.send(message["text"])
            elif message.get("bytes") is not None:
                await connection.send(message["bytes"])

    async def _upstream_to_container(self, connection, send) -> None:
        while True:
            try:
                message = await connection.recv()
            except ConnectionClosed as closed:
                if closed.rcvd is not None:
                    code, reason = closed.rcvd.code, closed.rcvd.reason
                    if code in _LOCAL_ONLY_CLOSE_CODES:
                        code, reason = 1011, ""
                else:
                    code, reason = 1011, ""
                # The other pump may have ended the container side first.
                with suppress(RuntimeError):
                    await send({"type": "websocket.close", "code": code, "reason": reason})
                return
            if isinstance(message, str):
                await send({"type": "websocket.send", "text": message})
            else:
                await send({"type": "websocket.send", "bytes": message})


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

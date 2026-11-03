"""Web-side LLM key-replacement proxy for the untrusted job container.

Authenticates a per-job placeholder, swaps in the real provider credential, and
streams the response. Listens on an internal port, unpublished to the host.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
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

from openscientist.job_container.secrets import RunKind, proxy_run
from openscientist.listener import bind_listener
from openscientist.llm_metering import (
    _model_from_body,
    _model_from_path,
    _ResponsesDecoder,
    _usage_decoder,
    record_model_call,
)
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


def _meter(
    *, job_id: str, kind: RunKind, decoder, provider_id: str, status_code: int
) -> Callable[[str], Awaitable[None]]:
    """The response-end meter of one forwarded request. Whatever ends the
    response, a clean end, an upstream cut, or a client that goes away, the
    row the wire's state table names for that ending is written, and an
    upstream error status writes no row and only a log line."""

    async def meter(outcome: str) -> None:
        if status_code >= 400:
            logger.warning(
                "LLM proxy: upstream returned %d for run %s (%s), so no cost row is written",
                status_code,
                job_id,
                kind,
            )
            return
        calls, reason = decoder.finalize(outcome)
        for call in calls:
            await record_model_call(
                job_id=job_id,
                kind=kind,
                model=call.model,
                provider_id=provider_id,
                buckets=call.buckets,
            )
        if reason:
            logger.warning("LLM proxy: %s for run %s (%s)", reason, job_id, kind)

    return meter


class _MeteredStreamResponse(Response):
    """The upstream's body forwarded unchanged while its decoder meters it.

    The row is written once the response is done, whatever ends it. The
    finally runs on a clean end, on an upstream cut, and on a client
    disconnect, which arrives as the cancellation of this task, and a
    cancelled task still runs its remaining awaits before it dies.
    """

    def __init__(
        self,
        *,
        upstream: httpx.Response,
        decoder,
        meter: Callable[[str], Awaitable[None]],
        status_code: int,
        headers: dict[str, str],
    ) -> None:
        super().__init__(status_code=status_code, headers=headers)
        # Response init sizes the header for its empty body buffer, but the
        # body is streamed through the sends below, so drop it again.
        self.raw_headers = [
            (name, value) for name, value in self.raw_headers if name.lower() != b"content-length"
        ]
        self._upstream = upstream
        self._decoder = decoder
        self._meter = meter

    async def __call__(self, scope, receive, send) -> None:
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        outcome = "cut"
        try:
            async for chunk in self._upstream.aiter_raw():
                self._decoder.feed(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            outcome = "end"
        finally:
            with suppress(Exception):
                await self._upstream.aclose()
            await self._meter(outcome)


def create_llm_proxy_app(
    *,
    upstream: Callable[[], LlmUpstream],
    client: httpx.AsyncClient,
    provider_id: Callable[[], str] | None = None,
) -> Starlette:
    """Build the proxy ASGI app. The callables are resolved per request, and only
    a live run's credential is accepted. ``provider_id`` resolves the active
    provider's id, which every metered row is attributed to."""

    # The configured id, read from settings: building the provider to ask it
    # for the id would validate its credentials, which a metered row must not
    # depend on.
    provider = provider_id or (lambda: get_settings().provider.provider_id)

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
        # Only a POST to a model-call path is a metered call. A count-tokens
        # lookup and a GET catalogue answer are not, and write no row.
        decoder = None
        if request.method == "POST":
            model = _model_from_body(body) or _model_from_path(request.url.path)
            decoder = _usage_decoder(
                request.url.path, upstream_resp.headers.get("content-type", ""), model
            )
        if decoder is None:
            return StreamingResponse(
                upstream_resp.aiter_raw(),
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                background=BackgroundTask(upstream_resp.aclose),
            )
        meter = _meter(
            job_id=run.job_id,
            kind=run.kind,
            decoder=decoder,
            provider_id=provider(),
            status_code=upstream_resp.status_code,
        )
        return _MeteredStreamResponse(
            upstream=upstream_resp,
            decoder=decoder,
            meter=meter,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    terminator = _ResponsesWebsocketTerminator(
        upstream=upstream, client=client, provider_id=provider
    )
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
    close codes propagated. Each ``response.completed`` frame meters one call,
    one row per completed response in a turn. Raw ASGI, so a dead run or an
    unusable credential is refused with an HTTP status before the upstream is
    touched.
    """

    def __init__(
        self,
        *,
        upstream: Callable[[], LlmUpstream],
        client: httpx.AsyncClient,
        provider_id: Callable[[], str],
    ) -> None:
        self._upstream = upstream
        self._client = client
        self._provider_id = provider_id

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
        decoder = _ResponsesDecoder("unknown")
        loop = asyncio.get_running_loop()
        to_upstream = loop.create_task(self._container_to_upstream(connection, receive))
        to_container = loop.create_task(
            self._upstream_to_container(connection, send, decoder, run.job_id, run.kind)
        )
        await asyncio.wait({to_upstream, to_container}, return_when=asyncio.FIRST_COMPLETED)
        for task in (to_upstream, to_container):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        with suppress(Exception):
            await connection.close()
        if not decoder.saw_completion():
            logger.warning(
                "LLM proxy: responses websocket for run %s (%s) ended without a completed response",
                run.job_id,
                run.kind,
            )

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

    async def _upstream_to_container(
        self, connection, send, decoder: _ResponsesDecoder, job_id: str, kind: RunKind
    ) -> None:
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
                data, key = message.encode("utf-8"), "text"
            elif isinstance(message, bytes):
                data, key = message, "bytes"
            else:
                continue
            # Meter the completed responses this frame reports, then
            # forward the frame unchanged.
            decoder.feed(data)
            for call in decoder.take_completed():
                await record_model_call(
                    job_id=job_id,
                    kind=kind,
                    model=call.model,
                    provider_id=self._provider_id(),
                    buckets=call.buckets,
                )
            await send({"type": "websocket.send", key: message})


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


def _active_provider_id() -> str:
    """The id of the active provider, which every metered row is attributed to."""
    return get_settings().provider.provider_id


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
    app = create_llm_proxy_app(
        upstream=_active_upstream, client=client, provider_id=_active_provider_id
    )
    config = uvicorn.Config(app, log_level="warning")
    _proxy_server = _NoSignalServer(config)
    _proxy_task = asyncio.create_task(_proxy_server.serve(sockets=[sock]))
    logger.info("LLM key-replacement proxy listening on port %d", port)

"""Web-side LLM key-replacement proxy for the untrusted job container.

Authenticates a per-job placeholder, swaps in the real provider credential, and
streams the response. Listens on an internal port, unpublished to the host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from urllib.parse import unquote_to_bytes
from uuid import UUID

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from openscientist.budget import check_budget
from openscientist.job_container.secrets import RunKind, proxy_run
from openscientist.listener import bind_listener
from openscientist.llm_metering import (
    _model_from_body,
    _model_from_path,
    _ObservedCall,
    _ResponsesDecoder,
    _usage_decoder,
    record_model_call,
)
from openscientist.providers import get_provider
from openscientist.providers.base import LlmUpstream, UpstreamCredentialError
from openscientist.request_body import read_request_body
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

_WEB_HOST_ENV = "OPENSCIENTIST_WEB_HOST"
_DEFAULT_WEB_HOST = "openscientist"
# https://platform.claude.com/docs/en/api/errors#request-size-limits
# https://developers.openai.com/api/docs/guides/production-best-practices#compress-request-bodies
_MAX_REQUEST_BYTES = 128 * 1024 * 1024

# Main agent plus three parallel subagents.
MAX_JOB_MODEL_CALLS = 4


class _Admissions:
    def __init__(self) -> None:
        self.active: dict[str, int] = {}
        self.settlements: set[asyncio.Task[None]] = set()

    def acquire(self, job_id: str) -> _Admission | None:
        count = self.active.get(job_id, 0)
        if count >= MAX_JOB_MODEL_CALLS:
            return None
        self.active[job_id] = count + 1
        return _Admission(self, job_id)

    async def settle(self, admission: _Admission, meter: Awaitable[None]) -> None:
        async def finish() -> None:
            try:
                async with asyncio.timeout(30):
                    await meter
            except Exception:
                logger.exception("LLM proxy could not meter run %s", admission.job_id)
            finally:
                admission.release()

        task = asyncio.create_task(finish())
        self.settlements.add(task)
        task.add_done_callback(self.settlements.discard)
        await asyncio.shield(task)


class _Admission:
    def __init__(self, owner: _Admissions, job_id: str) -> None:
        self.owner = owner
        self.job_id = job_id
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        remaining = self.owner.active[self.job_id] - 1
        if remaining:
            self.owner.active[self.job_id] = remaining
        else:
            del self.owner.active[self.job_id]


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


def _endpoint_allowed(
    provider_id: str, method: str, path: str, raw_path: bytes, *, websocket: bool = False
) -> bool:
    """Allow only the provider's harness inference and discovery endpoints."""
    if "\\" in path or "%" in path or any(segment in {".", ".."} for segment in path.split("/")):
        return False
    if provider_id == "bedrock" and not websocket:
        # https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_InvokeModel.html
        route = re.fullmatch(
            rb"/model/([^/]+)/(?:invoke|invoke-with-response-stream)", raw_path or path.encode()
        )
        if method != "POST" or route is None:
            return False
        try:
            model = unquote_to_bytes(route[1]).decode("ascii")
        except UnicodeDecodeError:
            return False
        return (
            re.fullmatch(
                r"(?:[A-Za-z0-9][A-Za-z0-9._:-]*|"
                r"arn:aws:bedrock:[a-z0-9-]+:"
                r"(?:[0-9]{12}:(?:inference-profile|application-inference-profile)|"
                r"(?:[0-9]{12})?:foundation-model)/[A-Za-z0-9][A-Za-z0-9._:-]*)",
                model,
            )
            is not None
        )
    if re.search(rb"%(?:2f|5c|25)", raw_path, re.IGNORECASE):
        return False
    if websocket:
        # https://developers.openai.com/api/docs/guides/websocket-mode
        return provider_id in {"openai", "azure-openai"} and path == "/responses"
    if provider_id in {"anthropic", "cborg", "foundry"}:
        # https://platform.claude.com/docs/en/api/messages and /messages/count_tokens
        return (method, path) in {
            ("POST", "/v1/messages"),
            ("POST", "/v1/messages/count_tokens"),
            ("GET", "/v1/models"),
            # The bundled Claude Code CLI probes connectivity here before sending messages.
            ("HEAD", "/api/hello"),
        }
    if provider_id in {"openai", "azure-openai"}:
        # https://developers.openai.com/api/reference/resources/responses
        return (method, path) in {
            ("POST", "/responses"),
            ("POST", "/responses/compact"),
            ("GET", "/models"),
        }
    if provider_id in {"ollama", "vllm", "llamacpp", "github-copilot"}:
        # SelfHostedOpenAiWire.omp_model_catalog uses the OpenAI chat-completions API.
        return (method, path) in {("POST", "/chat/completions"), ("GET", "/models")}
    if provider_id == "vertex":
        # https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai
        project = get_settings().provider.anthropic_vertex_project_id
        return (
            bool(project)
            and method == "POST"
            and re.fullmatch(
                rf"/projects/{re.escape(project or '')}/locations/[a-z0-9-]+/"
                r"publishers/anthropic/models/[A-Za-z0-9._@-]+"
                r"(?::rawPredict|:streamRawPredict)",
                path,
            )
            is not None
        )
    return False


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
    """Stream upstream bytes while holding admission through cancellation-safe metering."""

    def __init__(
        self,
        *,
        upstream: httpx.Response,
        decoder,
        meter: Callable[[str], Awaitable[None]],
        admission: _Admission | None = None,
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
        self._admission = admission

    async def __call__(self, scope, receive, send) -> None:
        outcome = "cut"

        async def finish() -> None:
            with suppress(Exception):
                await self._upstream.aclose()
            await self._meter(outcome)

        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            async for chunk in self._upstream.aiter_raw():
                self._decoder.feed(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            outcome = "end"
        finally:
            if self._admission is None:
                await finish()
            else:
                await self._admission.owner.settle(self._admission, finish())


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
    admissions = _Admissions()

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

        if not _endpoint_allowed(
            provider(), request.method, request.url.path, request.scope.get("raw_path", b"")
        ):
            logger.warning(
                "LLM endpoint refused for run %s (%s) at %s",
                run.job_id,
                run.kind,
                request.url.path,
            )
            return Response("LLM endpoint not allowed", status_code=403)

        try:
            body = await read_request_body(request, limit=_MAX_REQUEST_BYTES)
        except HTTPException as exc:
            logger.warning(
                "LLM request refused for run %s at %s with %s",
                run.job_id,
                request.url.path,
                exc.detail,
            )
            return Response(str(exc.detail), status_code=exc.status_code)
        model: str | None = None
        admission = None
        transferred = False
        if request.method == "POST":
            model = _model_from_body(body) or _model_from_path(request.url.path)
            if not request.url.path.endswith(("/count_tokens", "/count-tokens:rawPredict")):
                admission = admissions.acquire(run.job_id)
                if admission is None:
                    logger.warning(
                        "LLM proxy admission refused for run %s at %s", run.job_id, request.url.path
                    )
                    return Response("Job model call admission unavailable", status_code=429)
        try:
            if admission is not None:
                verdict = await check_budget(
                    job_id=UUID(run.job_id),
                    provider_id=provider(),
                    model=model,
                    include_unpriced=model != "unknown",
                )
                if not verdict.ok:
                    logger.warning(
                        "LLM proxy refused run %s at %s with %s",
                        run.job_id,
                        request.url.path,
                        verdict.message,
                    )
                    return Response(verdict.message, status_code=403)
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in _DROP_REQUEST_HEADERS and not k.lower().startswith("x-goog-")
            }
            path = (
                request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
                if provider() == "bedrock"
                else request.url.path
            )
            try:
                headers.update(
                    target.signing.credential_headers(
                        method=request.method,
                        path=path,
                        query=request.url.query,
                        body=body,
                    )
                )
            except UpstreamCredentialError:
                logger.warning("LLM proxy upstream credential unusable for run %s", run.job_id)
                return Response("upstream credential unavailable", status_code=401)
            url = target.signing.upstream_url(
                base_url=target.base_url, path=path, query=request.url.query
            )
            upstream_req = client.build_request(request.method, url, headers=headers, content=body)
            try:
                upstream_resp = await client.send(upstream_req, stream=True)
            except (Exception, asyncio.CancelledError):
                logger.warning(
                    "LLM proxy call ended before response headers for run %s (%s)",
                    run.job_id,
                    run.kind,
                )
                raise
            resp_headers = {
                k: v
                for k, v in upstream_resp.headers.items()
                if k.lower() not in _DROP_RESPONSE_HEADERS
            }
            decoder = None
            if request.method == "POST" and model is not None:
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
            response = _MeteredStreamResponse(
                upstream=upstream_resp,
                decoder=decoder,
                meter=meter,
                admission=admission,
                status_code=upstream_resp.status_code,
                headers=resp_headers,
            )
            transferred = True
            return response
        finally:
            if admission is not None and not transferred:
                admission.release()

    terminator = _ResponsesWebsocketTerminator(
        upstream=upstream, client=client, provider_id=provider, admissions=admissions
    )
    return Starlette(
        routes=[
            WebSocketRoute("/{path:path}", terminator),
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
    web-side credential, and pumps frames in both directions with close codes
    propagated. Each ``response.completed`` frame meters one call, one row per
    completed response in a turn, and a frame that starts a response on an
    unpriced model is refused before it is forwarded. Raw ASGI, so a dead run
    or an unusable credential is refused with an HTTP status before the
    upstream is touched.
    """

    def __init__(
        self,
        *,
        upstream: Callable[[], LlmUpstream],
        client: httpx.AsyncClient,
        provider_id: Callable[[], str],
        admissions: _Admissions,
    ) -> None:
        self._upstream = upstream
        self._client = client
        self._provider_id = provider_id
        self._admissions = admissions

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
        if not _endpoint_allowed(
            self._provider_id(), "GET", scope["path"], scope.get("raw_path", b""), websocket=True
        ):
            logger.warning(
                "LLM endpoint refused for run %s (%s) at %s",
                run.job_id,
                run.kind,
                scope["path"],
            )
            await _refuse_websocket(send, 403, "LLM endpoint not allowed")
            return
        verdict = await check_budget(job_id=UUID(run.job_id), provider_id=self._provider_id())
        if not verdict.ok:
            logger.warning("LLM proxy refused run %s with %s", run.job_id, verdict.message)
            await _refuse_websocket(send, 403, verdict.message or "Budget exhausted")
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
        forwarded = {
            k: v
            for k, v in headers.items()
            if k not in _WS_DROP_HEADERS and not k.startswith("x-goog-")
        }
        forwarded.update(signing_headers)
        try:
            connection = await ws_connect(
                url,
                additional_headers=forwarded,
                proxy=None,
                max_size=_MAX_REQUEST_BYTES,
                max_queue=1,
            )
        except Exception as exc:
            logger.warning("LLM proxy: upstream websocket unreachable (%s)", exc)
            await send({"type": "websocket.close", "code": 1011})
            return
        active: dict[str, _Admission] = {}
        tasks: list[asyncio.Task[None]] = []
        decoder = _ResponsesDecoder("unknown")
        try:
            await send({"type": "websocket.accept"})
            tasks = [
                asyncio.create_task(
                    self._container_to_upstream(connection, receive, send, run.job_id, active)
                ),
                asyncio.create_task(
                    self._upstream_to_container(
                        connection, send, decoder, run.job_id, run.kind, active
                    )
                ),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            try:
                with suppress(Exception):
                    await connection.close()
            finally:
                for admission in active.values():
                    logger.warning("LLM proxy stream ended without usage for run %s", run.job_id)
                    admission.release()

    async def _container_to_upstream(
        self, connection, receive, send, job_id: str, active: dict[str, _Admission]
    ) -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                code = message.get("code")
                reason = message.get("reason") or ""
                if code is None or code in _LOCAL_ONLY_CLOSE_CODES:
                    code, reason = 1011, ""
                await connection.close(code, reason)
                return
            payload = message.get("text")
            if payload is None:
                payload = message.get("bytes")
            if payload is None:
                continue
            if (
                len(payload) > _MAX_REQUEST_BYTES
                or isinstance(payload, str)
                and len(payload.encode("utf-8")) > _MAX_REQUEST_BYTES
            ):
                await connection.close(1009, "Request body too large")
                with suppress(RuntimeError):
                    await send(
                        {
                            "type": "websocket.close",
                            "code": 1009,
                            "reason": "Request body too large",
                        }
                    )
                return
            refusal = await self._call_refusal(payload, job_id, active)
            if refusal is not None:
                status, detail = refusal
                logger.warning("LLM proxy refused run %s with %s", job_id, detail)
                # Codex surfaces a pre-stream error frame as a failed turn.
                with suppress(RuntimeError):
                    await send(
                        {
                            "type": "websocket.send",
                            "text": json.dumps(
                                {"type": "error", "status": status, "error": {"message": detail}}
                            ),
                        }
                    )
                await connection.close(1008, detail[:120])
                return
            await connection.send(payload)

    async def _call_refusal(
        self, payload: str | bytes, job_id: str, active: dict[str, _Admission]
    ) -> tuple[int, str] | None:
        """Reserve admission before checking each response creation frame."""
        try:
            frame = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(frame, dict) or frame.get("type") != "response.create":
            return None
        stream = frame.get("stream_id", "")
        if not isinstance(stream, str):
            return 400, "Invalid response stream"
        admission = self._admissions.acquire(job_id)
        if admission is None:
            return 429, "Job model call admission unavailable"
        retained = False
        try:
            model = frame.get("model")
            verdict = await check_budget(
                job_id=UUID(job_id),
                provider_id=self._provider_id(),
                model=model if isinstance(model, str) and model else None,
                include_unpriced=True,
            )
            if not verdict.ok:
                return 403, verdict.message or "Budget exhausted"
            if stream in active:
                return 429, "Response stream already active"
            active[stream] = admission
            retained = True
            return None
        finally:
            if not retained:
                admission.release()

    async def _upstream_to_container(
        self,
        connection,
        send,
        decoder: _ResponsesDecoder,
        job_id: str,
        kind: RunKind,
        active: dict[str, _Admission],
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
            try:
                frame = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                frame = None
            terminal = isinstance(frame, dict) and frame.get("type") in {
                "response.completed",
                "response.failed",
                "response.incomplete",
                "error",
            }
            stream = frame.get("stream_id", "") if isinstance(frame, dict) else ""
            admission = active.pop(stream, None) if terminal and isinstance(stream, str) else None
            if isinstance(frame, dict) and frame.get("type") in {
                "response.failed",
                "response.incomplete",
            }:
                response = frame.get("response")
                if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                    frame["type"] = "response.completed"
                    data = json.dumps(frame).encode()
            decoder.feed_frame(data)
            calls = decoder.take_completed()

            meter = self._record_calls(calls, terminal=terminal, job_id=job_id, kind=kind)

            if admission is None:
                await meter
            else:
                await self._admissions.settle(admission, meter)
            await send({"type": "websocket.send", key: message})

    async def _record_calls(
        self, calls: list[_ObservedCall], *, terminal: bool, job_id: str, kind: RunKind
    ) -> None:
        for call in calls:
            await record_model_call(
                job_id=job_id,
                kind=kind,
                model=call.model,
                provider_id=self._provider_id(),
                buckets=call.buckets,
            )
        if terminal and not calls:
            logger.warning("LLM proxy response ended without usage for run %s", job_id)


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
    config = uvicorn.Config(
        app, log_level="warning", ws_max_size=_MAX_REQUEST_BYTES, ws_max_queue=1
    )
    _proxy_server = _NoSignalServer(config)
    _proxy_task = asyncio.create_task(_proxy_server.serve(sockets=[sock]))
    logger.info("LLM key-replacement proxy listening on port %d", port)

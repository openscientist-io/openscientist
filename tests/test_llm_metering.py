"""The LLM proxy meters every model call it forwards, one cost_records row per
call at response end, written by the web. The container writes no cost rows
(plans/llm-proxy-metering.md, the state table of its section 4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
)
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from uvicorn.config import Config
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from openscientist.database.models import CostRecord, Job
from openscientist.job_container.secrets import (
    RunCredentials,
    RunKind,
    issue_run_credentials,
    revoke_run_credentials,
)
from openscientist.llm_proxy import _NoSignalServer, create_llm_proxy_app
from openscientist.providers.base import LlmUpstream, StaticHeadersSigning
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

_PROVIDER = "anthropic"
_PRICED_MODEL = "stub-sonnet"
_BEDROCK_MODEL = "amazon.nova-lite-v1"
_UNPRICED_MODEL = "stub-no-rate"


@pytest.fixture(autouse=True)
def wire_provider(request, monkeypatch):
    providers = {
        "TestOpenAiChatWire": "ollama",
        "TestResponsesWire": "openai",
        "TestResponsesWebsocket": "openai",
        "TestBedrockWire": "bedrock",
        "TestVertexWire": "vertex",
        "TestNotMetered": "vertex",
    }
    monkeypatch.setattr(
        "tests.test_llm_metering._PROVIDER", providers.get(request.cls.__name__, "anthropic")
    )
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
    from openscientist.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_RATES = {
    "input_cost_per_token": 3e-6,
    "output_cost_per_token": 15e-6,
    "cache_read_input_token_cost": 0.3e-6,
    "cache_creation_input_token_cost": 3.75e-6,
    "cache_creation_input_token_cost_above_1hr": 6e-6,
    "max_output_tokens": 100,
}

_PRICING = {
    _PRICED_MODEL: _RATES,
    # The Bedrock row is metered by the model in the path, so the stub card
    # must price that name at the same rates.
    _BEDROCK_MODEL: _RATES,
}


def _cost(
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    cache_write_1h: int = 0,
    reasoning: int = 0,
) -> float:
    """The stubbed rate card applied to the buckets, the price a row must hold."""
    rates = _PRICING[_PRICED_MODEL]
    return (
        rates["input_cost_per_token"] * input_tokens
        + rates["output_cost_per_token"] * output_tokens
        + rates["cache_read_input_token_cost"] * cache_read
        + rates["cache_creation_input_token_cost"] * cache_write
        + rates["cache_creation_input_token_cost_above_1hr"] * cache_write_1h
        + rates["output_cost_per_token"] * reasoning
    )


@pytest.fixture(autouse=True)
def stubbed_pricing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pricing reads a fixed dict, so a row's cost is what its buckets cost at
    known rates, with no network and no 24 h cache inherited from other tests."""
    monkeypatch.setattr(
        "openscientist.providers.pricing._get_litellm_pricing", lambda: dict(_PRICING)
    )
    yield


@pytest_asyncio.fixture
async def job(test_engine: AsyncEngine) -> AsyncGenerator[UUID, None]:
    """A running job at iteration 3, committed so the web's writer sees it."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        row = Job(research_question="Q?", status="running", current_iteration=3)
        session.add(row)
        await session.commit()
    yield row.id
    async with test_engine.begin() as conn:
        await conn.execute(delete(Job).where(Job.id == row.id))


@pytest.fixture
def run_for() -> Iterator[Callable[[str, RunKind], RunCredentials]]:
    """Issue a live run's credentials for a job and a kind, ended after the test."""
    issued: list[str] = []

    def issue(job_id: str, kind: RunKind) -> RunCredentials:
        role = f"agent_{uuid4().hex}"
        issued.append(role)
        return issue_run_credentials(job_id, role, kind)

    yield issue
    for role in issued:
        revoke_run_credentials(role)


async def _rows(test_engine: AsyncEngine, job_id: UUID) -> list[tuple]:
    """The job's cost rows as (operation, iteration, provider, model, in, out,
    cache_read, cache_write_total, reasoning, cost), in the order written."""
    async with test_engine.connect() as conn:
        result = await conn.execute(
            select(
                CostRecord.operation_type,
                CostRecord.iteration,
                CostRecord.provider,
                CostRecord.model,
                CostRecord.input_tokens,
                CostRecord.output_tokens,
                CostRecord.cache_read_tokens,
                CostRecord.cache_write_tokens,
                CostRecord.reasoning_tokens,
                CostRecord.cost_usd,
            )
            .where(CostRecord.job_id == job_id)
            .order_by(CostRecord.id)
        )
        return [tuple(row) for row in result]


@dataclass
class _Plan:
    """The stubbed upstream's answer for one path: status, content type, the
    body as chunks, and optionally the chunk count after which the stream
    hangs, which models an upstream that never finishes."""

    status: int = 200
    content_type: str = "application/json"
    chunks: list[bytes] = field(default_factory=list)
    hang_after: int | None = None
    hold_response: bool = False
    blocked: asyncio.Event = field(default_factory=asyncio.Event)


def _upstream_handler(
    plans: dict[str, _Plan],
) -> Callable[[Request], Awaitable[StreamingResponse]]:
    async def handler(request: Request) -> StreamingResponse:
        plan = _match(request.url.path, plans)
        if plan.hold_response:
            plan.blocked.set()
            await asyncio.Event().wait()

        async def stream() -> AsyncGenerator[bytes, None]:
            for index, chunk in enumerate(plan.chunks):
                yield chunk
                if plan.hang_after is not None and index + 1 >= plan.hang_after:
                    plan.blocked.set()
                    await asyncio.Event().wait()

        return StreamingResponse(
            stream(), status_code=plan.status, headers={"content-type": plan.content_type}
        )

    return handler


def _match(path: str, plans: dict[str, _Plan]) -> _Plan:
    for suffix, plan in plans.items():
        if path.endswith(suffix):
            return plan
    raise AssertionError(f"stub upstream got an unplanned path {path}")


class _StreamingASGITransport(httpx.AsyncBaseTransport):
    """The ASGI upstream, but streaming where httpx's transport buffers.

    httpx's ``ASGITransport`` only returns once the app's whole response has
    finished, so an upstream that hangs between chunks would hang ``client.send``
    itself, and the proxy could never be observed mid-forward. This one hands
    back the response at its start and feeds the body as the app sends it, and
    closing the stream cancels the app, the way a gone client reaches a real
    upstream."""

    def __init__(self, app: Starlette) -> None:
        self._app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": "https",
            "path": request.url.path,
            "raw_path": request.url.raw_path,
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
            "headers": request.headers.raw,
        }
        body = request.read()

        state = {"body_sent": False}

        async def receive() -> dict:
            if not state["body_sent"]:
                state["body_sent"] = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        start: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def send(message: Mapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                await start.put(message)
            elif message["type"] == "http.response.body":
                await chunks.put(message.get("body", b""))
                if not message.get("more_body", True):
                    await chunks.put(None)

        app_task = asyncio.create_task(self._app(scope, receive, send))
        app_task.add_done_callback(
            lambda _: (
                chunks.put_nowait(None)
                if not app_task.cancelled() and chunks.qsize() == 0
                else None
            )
        )

        class _Stream(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self._closed = False

            async def __aiter__(self) -> AsyncIterator[bytes]:
                while True:
                    chunk = await chunks.get()
                    if chunk is None:
                        break
                    yield chunk

            async def aclose(self) -> None:
                if self._closed:
                    return
                self._closed = True
                chunks.put_nowait(None)
                app_task.cancel()
                with contextlib.suppress(BaseException):
                    await app_task

        try:
            start_message = await start.get()
        except BaseException:
            app_task.cancel()
            with contextlib.suppress(BaseException):
                await app_task
            raise
        headers = httpx.Headers(
            [(key.decode(), value.decode()) for key, value in start_message.get("headers", [])]
        )
        return httpx.Response(
            start_message["status"], headers=headers, request=request, stream=_Stream()
        )


def _upstream_client(plans: dict[str, _Plan]) -> httpx.AsyncClient:
    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                _upstream_handler(plans),
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ]
    )
    return httpx.AsyncClient(transport=_StreamingASGITransport(app))


def _proxy_app(upstream_client: httpx.AsyncClient) -> Starlette:
    return create_llm_proxy_app(
        upstream=lambda: LlmUpstream(
            "https://upstream.test",
            StaticHeadersSigning({"x-api-key": "REAL-KEY"}),
        ),
        client=upstream_client,
        provider_id=lambda: _PROVIDER,
    )


def _estimated_row(body: dict | bytes, model: str = _PRICED_MODEL) -> tuple:
    wire = body if isinstance(body, bytes) else json.dumps(body).encode()
    return (
        "discovery_estimated",
        3,
        _PROVIDER,
        model,
        len(wire),
        100,
        0,
        0,
        0,
        _cost(len(wire), 100),
    )


def _post(app: Starlette, path: str, placeholder: str, body: dict | str) -> None:
    content = body if isinstance(body, str) else json.dumps(body)
    with TestClient(app) as client:
        client.post(
            path,
            headers={"x-api-key": placeholder, "content-type": "application/json"},
            content=content.encode(),
        )


async def _drive(
    app: Starlette,
    *,
    path: str,
    placeholder: str,
    body: bytes,
    blocked: asyncio.Event,
) -> None:
    """Cancel one request after its upstream blocks and await its cleanup."""
    sent: list[Mapping[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"localhost"),
            (b"x-api-key", placeholder.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8081),
    }
    state = {"request_done": False}

    async def receive() -> dict:
        if not state["request_done"]:
            state["request_done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)

    task = asyncio.create_task(app(scope, receive, send))
    try:
        async with asyncio.timeout(10):
            await blocked.wait()
    finally:
        task.cancel()
        async with asyncio.timeout(10):
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _anthropic_usage(
    *,
    input_tokens: int = 100,
    output_tokens: int = 25,
    cache_write: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_write + cache_write_1h,
        "cache_creation": {"ephemeral_1h_input_tokens": cache_write_1h},
        "cache_read_input_tokens": cache_read,
    }


def _start_usage(usage: dict) -> dict:
    """``message_start`` carries only the input and cache buckets, the output
    count arriving with ``message_delta``."""
    return {key: value for key, value in usage.items() if key != "output_tokens"}


def _anthropic_sse_chunks(usage: dict, output: int, *, with_delta: bool = True) -> list[bytes]:
    events = [
        f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': 'm1', 'usage': _start_usage(usage)}})}\n\n",
        'event: content_block_delta\ndata: {"type": "content_block_delta"}\n\n',
    ]
    if with_delta:
        events.append(
            f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'usage': {'output_tokens': output}})}\n\n"
        )
    events.append('event: message_stop\ndata: {"type": "message_stop"}\n\n')
    return [event.encode() for event in events]


def _chat_usage(
    *, prompt: int = 120, completion: int = 30, cached: int = 40, reasoning: int = 6
) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


def _chat_sse_chunks(usage: dict | None) -> list[bytes]:
    chunks = [
        f"data: {json.dumps({'id': 'c1', 'choices': [{'delta': {'content': 'hi'}}]})}\n\n".encode()
    ]
    if usage is not None:
        chunks.append(
            f"data: {json.dumps({'id': 'c1', 'choices': [], 'usage': usage})}\n\n".encode()
        )
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def _responses_frame(kind: str, response: dict) -> bytes:
    payload = json.dumps({"type": kind, "response": response})
    return f"event: {kind.removesuffix('_')}\ndata: {payload}\n\n".encode()


def _responses_sse_chunks(response: dict) -> list[bytes]:
    return [
        _responses_frame("response.created", {"id": "r1"}),
        _responses_frame("response.completed", response),
    ]


def _eventstream_frame(payload: dict) -> bytes:
    """One binary eventstream frame: the 20-byte prelude (total length, header
    length, message type, payload length, prelude checksum), the empty headers,
    the payload, and the trailing message checksum, all-zero where the value
    does not matter here."""
    body = json.dumps(payload).encode()
    total = 20 + len(body) + 4
    return (
        struct.pack(">II", total, 0)
        + struct.pack(">II", 0, len(body))
        + b"\x00" * 4
        + body
        + b"\x00" * 4
    )


def _bedrock_stream_chunks(usage: dict, output: int) -> list[bytes]:
    frames: list[dict[str, object]] = [
        {"type": "message_start", "message": {"id": "m1", "usage": _start_usage(usage)}},
        {"type": "content_block_delta", "delta": {"type": "text_delta"}},
        {"type": "message_delta", "usage": {"output_tokens": output}},
        {"type": "metadata"},
    ]
    return [_eventstream_frame(frame) for frame in frames]


class TestAnthropicWire:
    """Rows A and B, the Anthropic Messages JSON and SSE the proxy also sees
    from CBORG, Foundry and the self-hosted Anthropic gateways."""

    async def test_count_tokens_writes_no_cost(self, test_engine, job, run_for):
        placeholder = run_for(str(job), "discovery").llm_placeholder
        plan = _Plan(chunks=[b'{"input_tokens": 12}'])
        app = _proxy_app(_upstream_client({"/v1/messages/count_tokens": plan}))
        _post(app, "/v1/messages/count_tokens", placeholder, {"model": _PRICED_MODEL})
        assert await _rows(test_engine, job) == []

    @pytest.mark.asyncio
    async def test_a_non_streaming_answer_writes_its_full_row(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {
            "model": _PRICED_MODEL,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }
        plan = _Plan(chunks=[json.dumps({"id": "m1", "usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert await _rows(test_engine, job) == [
            ("discovery", 3, _PROVIDER, _PRICED_MODEL, 100, 25, 0, 0, 0, _cost(100, 25)),
        ]

    @pytest.mark.asyncio
    async def test_the_one_hour_cache_tier_is_split_and_priced(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        usage = _anthropic_usage(cache_write=30, cache_write_1h=10, cache_read=20)
        plan = _Plan(chunks=[json.dumps({"usage": usage}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _PRICED_MODEL,
                100,
                25,
                20,
                40,
                0,
                _cost(100, 25, cache_read=20, cache_write=30, cache_write_1h=10),
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_completed_stream_writes_its_final_usage(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "stream": True, "messages": []}
        plan = _Plan(
            content_type="text/event-stream",
            chunks=_anthropic_sse_chunks(_anthropic_usage(cache_read=20), output=44),
        )
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _PRICED_MODEL,
                100,
                44,
                20,
                0,
                0,
                _cost(100, 44, cache_read=20),
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_stream_cut_before_final_usage_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "stream": True, "messages": []}
        chunks = _anthropic_sse_chunks(_anthropic_usage(cache_read=20), output=44, with_delta=False)
        plan = _Plan(content_type="text/event-stream", chunks=chunks)
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_a_client_that_goes_away_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = json.dumps({"model": _PRICED_MODEL, "stream": True, "messages": []}).encode()
        chunks = _anthropic_sse_chunks(_anthropic_usage(cache_read=20), output=44)
        plan = _Plan(
            content_type="text/event-stream",
            chunks=chunks[:2],
            hang_after=2,
        )
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        await _drive(
            app, path="/v1/messages", placeholder=placeholder, body=body, blocked=plan.blocked
        )
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_a_non_streaming_answer_without_usage_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"id": "m1"}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_an_upstream_error_cancels_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(status=529, chunks=[json.dumps({"error": "overloaded"}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert await _rows(test_engine, job) == []

    @pytest.mark.asyncio
    async def test_cancellation_before_response_headers_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = json.dumps({"model": _PRICED_MODEL, "messages": []}).encode()
        plan = _Plan(hold_response=True)
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        await _drive(
            app, path="/v1/messages", placeholder=placeholder, body=body, blocked=plan.blocked
        )
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_an_unpriced_model_keeps_its_buckets_at_zero_cost(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _UNPRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert await _rows(test_engine, job) == [
            ("discovery", 3, _PROVIDER, _UNPRICED_MODEL, 100, 25, 0, 0, 0, 0.0),
        ]


class TestOpenAiChatWire:
    """Rows C and D, the OpenAI chat-completions JSON and SSE, the wire every
    OpenAI-compatible server speaks, self-hosted or hosted."""

    @pytest.mark.asyncio
    async def test_a_non_streaming_answer_writes_its_subtracted_buckets(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        usage = _chat_usage()
        plan = _Plan(chunks=[json.dumps({"id": "c1", "usage": usage}).encode()])
        app = _proxy_app(_upstream_client({"/chat/completions": plan}))
        _post(app, "/chat/completions", placeholder, body)
        # prompt minus the cached part, completion minus the reasoning part.
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _PRICED_MODEL,
                80,
                24,
                40,
                0,
                6,
                _cost(80, 24, cache_read=40, reasoning=6),
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_completed_stream_with_final_usage_writes_its_row(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {
            "model": _PRICED_MODEL,
            "stream": True,
            "messages": [],
            "stream_options": {"include_usage": True},
        }
        plan = _Plan(content_type="text/event-stream", chunks=_chat_sse_chunks(_chat_usage()))
        app = _proxy_app(_upstream_client({"/chat/completions": plan}))
        _post(app, "/chat/completions", placeholder, body)
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _PRICED_MODEL,
                80,
                24,
                40,
                0,
                6,
                _cost(80, 24, cache_read=40, reasoning=6),
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_completed_stream_without_usage_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "stream": True, "messages": []}
        plan = _Plan(content_type="text/event-stream", chunks=_chat_sse_chunks(None))
        app = _proxy_app(_upstream_client({"/chat/completions": plan}))
        _post(app, "/chat/completions", placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_a_stream_cut_before_usage_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "stream": True, "messages": []}
        plan = _Plan(
            content_type="text/event-stream",
            chunks=_chat_sse_chunks(_chat_usage())[:1],
        )
        app = _proxy_app(_upstream_client({"/chat/completions": plan}))
        _post(app, "/chat/completions", placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_an_error_event_after_the_usage_still_writes_the_row(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "stream": True, "messages": []}
        error = f"data: {json.dumps({'error': {'message': 'rate limited'}})}\n\n".encode()
        chunks = _chat_sse_chunks(_chat_usage())[:-1] + [error]
        plan = _Plan(content_type="text/event-stream", chunks=chunks)
        app = _proxy_app(_upstream_client({"/chat/completions": plan}))
        _post(app, "/chat/completions", placeholder, body)
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _PRICED_MODEL,
                80,
                24,
                40,
                0,
                6,
                _cost(80, 24, cache_read=40, reasoning=6),
            ),
        ]


class TestResponsesWire:
    """Row E, the OpenAI Responses REST SSE. Usage is reported only at
    completion, so a cut or a failed stream writes no row, and the harness's
    retry meters itself."""

    async def test_compaction_records_responses_usage(self, test_engine, job, run_for):
        placeholder = run_for(str(job), "discovery").llm_placeholder
        usage = {
            "input_tokens": 100,
            "output_tokens": 25,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens_details": {"reasoning_tokens": 5},
        }
        plan = _Plan(chunks=[json.dumps({"usage": usage}).encode()])
        app = _proxy_app(_upstream_client({"/responses/compact": plan}))
        _post(app, "/responses/compact", placeholder, {"model": _PRICED_MODEL})
        rows = await _rows(test_engine, job)
        assert len(rows) == 1
        assert rows[0][4:9] == (80, 20, 20, 0, 5)

    @pytest.mark.asyncio
    async def test_a_completed_response_writes_its_row_at_its_own_model(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": "wire-model", "input": "hi"}
        response = {
            "id": "r1",
            "model": _PRICED_MODEL,
            "usage": {
                "input_tokens": 200,
                "output_tokens": 60,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens_details": {"reasoning_tokens": 10},
            },
        }
        plan = _Plan(content_type="text/event-stream", chunks=_responses_sse_chunks(response))
        app = _proxy_app(_upstream_client({"/responses": plan}))
        _post(app, "/responses", placeholder, body)
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _PRICED_MODEL,
                100,
                50,
                100,
                0,
                10,
                _cost(100, 50, cache_read=100, reasoning=10),
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_stream_cut_before_completion_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "input": "hi"}
        created = _responses_frame("response.created", {"id": "r1"})
        plan = _Plan(content_type="text/event-stream", chunks=[created])
        app = _proxy_app(_upstream_client({"/responses": plan}))
        _post(app, "/responses", placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_a_failed_response_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "input": "hi"}
        failed = _responses_frame("response.failed", {"id": "r1", "status": "failed"})
        plan = _Plan(content_type="text/event-stream", chunks=[failed])
        app = _proxy_app(_upstream_client({"/responses": plan}))
        _post(app, "/responses", placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_a_cut_stream_client_side_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = json.dumps({"model": _PRICED_MODEL, "input": "hi"}).encode()
        created = _responses_frame("response.created", {"id": "r1"})
        plan = _Plan(
            content_type="text/event-stream",
            chunks=[created],
            hang_after=1,
        )
        app = _proxy_app(_upstream_client({"/responses": plan}))
        await _drive(
            app, path="/responses", placeholder=placeholder, body=body, blocked=plan.blocked
        )
        assert await _rows(test_engine, job) == [_estimated_row(body)]


class TestBedrockWire:
    """Rows G and H, the Bedrock invoke JSON and the binary eventstream, which
    the gateway must pass through unconverted, so the proxy parses frames only
    to read the usage out of them and meters by the model in the path."""

    @pytest.mark.asyncio
    async def test_an_invoke_answer_writes_its_row_at_the_path_model(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = f"/model/{_BEDROCK_MODEL}/invoke"
        body = {"anthropic_version": "bedrock-2025-01-15", "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({path: plan}))
        _post(app, path, placeholder, body)
        assert await _rows(test_engine, job) == [
            ("discovery", 3, _PROVIDER, _BEDROCK_MODEL, 100, 25, 0, 0, 0, _cost(100, 25)),
        ]

    @pytest.mark.asyncio
    async def test_a_completed_eventstream_writes_its_row(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = f"/model/{_BEDROCK_MODEL}/invoke-with-response-stream"
        body = {"anthropic_version": "bedrock-2025-01-15", "messages": []}
        plan = _Plan(
            content_type="application/vnd.amazon.eventstream",
            chunks=_bedrock_stream_chunks(_anthropic_usage(cache_read=20), 44),
        )
        app = _proxy_app(_upstream_client({path: plan}))
        _post(app, path, placeholder, body)
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _BEDROCK_MODEL,
                100,
                44,
                20,
                0,
                0,
                _cost(100, 44, cache_read=20),
            ),
        ]

    @pytest.mark.asyncio
    async def test_an_eventstream_cut_after_its_start_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = f"/model/{_BEDROCK_MODEL}/invoke-with-response-stream"
        body = {"anthropic_version": "bedrock-2025-01-15", "messages": []}
        start = _eventstream_frame(
            {
                "type": "message_start",
                "message": {"id": "m1", "usage": _start_usage(_anthropic_usage(cache_read=20))},
            }
        )
        plan = _Plan(content_type="application/vnd.amazon.eventstream", chunks=[start])
        app = _proxy_app(_upstream_client({path: plan}))
        _post(app, path, placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body, _BEDROCK_MODEL)]

    @pytest.mark.asyncio
    async def test_an_eventstream_exception_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = f"/model/{_BEDROCK_MODEL}/invoke-with-response-stream"
        body = {"anthropic_version": "bedrock-2025-01-15", "messages": []}
        start = _eventstream_frame(
            {
                "type": "message_start",
                "message": {"id": "m1", "usage": _start_usage(_anthropic_usage(cache_read=20))},
            }
        )
        exception = _eventstream_frame({"type": "exception", "error": "Overloaded"})
        plan = _Plan(content_type="application/vnd.amazon.eventstream", chunks=[start, exception])
        app = _proxy_app(_upstream_client({path: plan}))
        _post(app, path, placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body, _BEDROCK_MODEL)]

    @pytest.mark.asyncio
    async def test_frames_pass_through_unchanged(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = f"/model/{_BEDROCK_MODEL}/invoke-with-response-stream"
        body = {"anthropic_version": "bedrock-2025-01-15", "messages": []}
        frames = _bedrock_stream_chunks(_anthropic_usage(), 44)
        plan = _Plan(content_type="application/vnd.amazon.eventstream", chunks=frames)
        app = _proxy_app(_upstream_client({path: plan}))
        with TestClient(app) as client:
            resp = client.post(path, headers={"x-api-key": placeholder}, content=json.dumps(body))
        assert resp.status_code == 200
        assert resp.content == b"".join(frames)


class TestVertexWire:
    """Rows I and J, the Vertex rawPredict and streamRawPredict, the Anthropic
    wire under the Vertex path, metered by the model in the path."""

    @pytest.mark.asyncio
    async def test_a_raw_predict_answer_writes_its_row_at_the_path_model(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = (
            "/projects/proj/locations/us-east5/publishers/anthropic/models/"
            f"{_PRICED_MODEL}@20250715:rawPredict"
        )
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({":rawPredict": plan}))
        _post(app, path, placeholder, body)
        assert await _rows(test_engine, job) == [
            ("discovery", 3, _PROVIDER, _PRICED_MODEL, 100, 25, 0, 0, 0, _cost(100, 25)),
        ]

    @pytest.mark.asyncio
    async def test_a_stream_raw_predict_writes_its_final_usage(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = (
            "/projects/proj/locations/us-east5/publishers/anthropic/models/"
            f"{_PRICED_MODEL}@20250715:streamRawPredict"
        )
        body = {"model": _PRICED_MODEL, "stream": True, "messages": []}
        plan = _Plan(
            content_type="text/event-stream",
            chunks=_anthropic_sse_chunks(_anthropic_usage(cache_read=20), output=44),
        )
        app = _proxy_app(_upstream_client({":streamRawPredict": plan}))
        _post(app, path, placeholder, body)
        assert await _rows(test_engine, job) == [
            (
                "discovery",
                3,
                _PROVIDER,
                _PRICED_MODEL,
                100,
                44,
                20,
                0,
                0,
                _cost(100, 44, cache_read=20),
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_cut_stream_raw_predict_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = (
            "/projects/proj/locations/us-east5/publishers/anthropic/models/"
            f"{_PRICED_MODEL}@20250715:streamRawPredict"
        )
        body = {"model": _PRICED_MODEL, "stream": True, "messages": []}
        chunks = _anthropic_sse_chunks(_anthropic_usage(cache_read=20), output=44, with_delta=False)
        plan = _Plan(content_type="text/event-stream", chunks=chunks)
        app = _proxy_app(_upstream_client({":streamRawPredict": plan}))
        _post(app, path, placeholder, body)
        assert await _rows(test_engine, job) == [_estimated_row(body)]


class TestResponsesWebsocket:
    """Meter completed websocket responses and retain estimates for interrupted calls."""

    @pytest.fixture
    async def stub_upstream(self):
        stub = _ResponsesWebsocketStub()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            stub.port = probe.getsockname()[1]
        async with serve(stub._handler, "127.0.0.1", stub.port):
            yield stub

    @pytest.fixture
    async def live_proxy(self, stub_upstream):
        client = httpx.AsyncClient(timeout=httpx.Timeout(None))
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                f"http://127.0.0.1:{stub_upstream.port}",
                StaticHeadersSigning({"authorization": "Bearer REAL-TOK"}),
            ),
            client=client,
            provider_id=lambda: _PROVIDER,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        server = _NoSignalServer(Config(app, log_level="warning"))
        task = asyncio.get_running_loop().create_task(server.serve(sockets=[sock]))
        for _ in range(200):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                    break
            except OSError:
                await asyncio.sleep(0.01)
        try:
            yield port
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            sock.close()
            await client.aclose()

    @pytest.mark.asyncio
    async def test_each_completed_response_writes_its_own_row(
        self, test_engine: AsyncEngine, job: UUID, run_for, stub_upstream, live_proxy
    ) -> None:
        frames = [
            _responses_frame("response.created", {"id": "r1"}),
            _responses_frame(
                "response.completed",
                {
                    "id": "r1",
                    "model": _PRICED_MODEL,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "input_tokens_details": {"cached_tokens": 30},
                        "output_tokens_details": {"reasoning_tokens": 4},
                    },
                },
            ),
            _responses_frame("response.created", {"id": "r2"}),
            _responses_frame(
                "response.completed",
                {
                    "id": "r2",
                    "model": _PRICED_MODEL,
                    "usage": {"input_tokens": 50, "output_tokens": 10},
                },
            ),
        ]
        # A copy, since the stub pops its list as it streams the frames out.
        stub_upstream.replies = list(frames)
        stub_upstream.close_after = 4
        placeholder = run_for(str(job), "chat").llm_placeholder
        async with ws_connect(
            f"ws://127.0.0.1:{live_proxy}/responses",
            additional_headers={"authorization": f"Bearer {placeholder}"},
            proxy=None,
        ) as connection:
            await connection.send(json.dumps({"type": "response.create", "model": _PRICED_MODEL}))
            received = [await connection.recv() for _ in range(4)]
            with contextlib.suppress(ConnectionClosed):
                await connection.recv()
        assert received == frames
        rows = await _rows(test_engine, job)
        assert rows == [
            (
                "chat",
                None,
                _PROVIDER,
                _PRICED_MODEL,
                70,
                16,
                30,
                0,
                4,
                _cost(70, 16, cache_read=30, reasoning=4),
            ),
            ("chat", None, _PROVIDER, _PRICED_MODEL, 50, 10, 0, 0, 0, _cost(50, 10)),
        ]

    @pytest.mark.asyncio
    async def test_a_turn_without_completion_keeps_the_estimate(
        self, test_engine: AsyncEngine, job: UUID, run_for, stub_upstream, live_proxy
    ) -> None:
        stub_upstream.replies = [_responses_frame("response.created", {"id": "r1"})]
        stub_upstream.close_after = 1
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"type": "response.create", "model": _PRICED_MODEL}
        async with ws_connect(
            f"ws://127.0.0.1:{live_proxy}/responses",
            additional_headers={"authorization": f"Bearer {placeholder}"},
            proxy=None,
        ) as connection:
            await connection.send(json.dumps(body))
            await connection.recv()
            with contextlib.suppress(ConnectionClosed):
                await connection.recv()
        async with asyncio.timeout(5):
            while not (rows := await _rows(test_engine, job)):
                await asyncio.sleep(0.01)
        assert rows == [_estimated_row(body)]

    @pytest.mark.asyncio
    async def test_raw_json_frames_meter_each_completed_response(
        self, test_engine: AsyncEngine, job: UUID, run_for, stub_upstream, live_proxy
    ) -> None:
        # The pinned codex wire: one raw JSON event per frame, no SSE
        # ``data:`` framing, and the last frame ends on a bare newline.
        frames = [
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "r1",
                        "model": _PRICED_MODEL,
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    },
                }
            ).encode(),
            (
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "r2",
                            "model": _PRICED_MODEL,
                            "usage": {"input_tokens": 50, "output_tokens": 10},
                        },
                    }
                )
                + "\n"
            ).encode(),
        ]
        # A copy, since the stub pops its list as it streams the frames out.
        stub_upstream.replies = list(frames)
        stub_upstream.close_after = 2
        placeholder = run_for(str(job), "chat").llm_placeholder
        async with ws_connect(
            f"ws://127.0.0.1:{live_proxy}/responses",
            additional_headers={"authorization": f"Bearer {placeholder}"},
            proxy=None,
        ) as connection:
            await connection.send(json.dumps({"type": "response.create", "model": _PRICED_MODEL}))
            received = [await connection.recv() for _ in range(2)]
            with contextlib.suppress(ConnectionClosed):
                await connection.recv()
        assert received == frames
        assert await _rows(test_engine, job) == [
            ("chat", None, _PROVIDER, _PRICED_MODEL, 100, 20, 0, 0, 0, _cost(100, 20)),
            ("chat", None, _PROVIDER, _PRICED_MODEL, 50, 10, 0, 0, 0, _cost(50, 10)),
        ]


class TestAttribution:
    """The row's labels. The job id and the kind ride the run credential, the
    provider is the active provider's id, and the operation type and the
    iteration come from the job's state at write time, which are display and
    breakdown only and never move a total."""

    @pytest.mark.asyncio
    async def test_a_discovery_run_labels_its_row_at_its_iteration(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert (await _rows(test_engine, job))[0][:3] == ("discovery", 3, _PROVIDER)

    @pytest.mark.asyncio
    async def test_a_run_in_its_report_phase_labels_its_row_a_report(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        async with test_engine.begin() as conn:
            await conn.execute(delete(CostRecord).where(CostRecord.job_id == job))
        from sqlalchemy import text

        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET status = 'generating_report' WHERE id = :id"), {"id": job}
            )
        placeholder = run_for(str(job), "discovery").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert (await _rows(test_engine, job))[0][:3] == ("report", 3, _PROVIDER)

    @pytest.mark.asyncio
    async def test_a_chat_run_labels_its_row_a_chat_without_iteration(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "chat").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert (await _rows(test_engine, job))[0][:3] == ("chat", None, _PROVIDER)

    @pytest.mark.asyncio
    async def test_a_report_kind_run_in_its_report_phase_labels_its_row_a_report(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        from sqlalchemy import text

        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET status = 'generating_report' WHERE id = :id"), {"id": job}
            )
        placeholder = run_for(str(job), "report").llm_placeholder
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        _post(app, "/v1/messages", placeholder, body)
        assert (await _rows(test_engine, job))[0][:3] == ("report", 3, _PROVIDER)


class TestNotMetered:
    """The paths that are not model calls. A count-tokens lookup and a GET
    catalogue answer pass through and write no row, and a dead run's
    placeholder still writes no row."""

    @pytest_asyncio.fixture
    async def counting_budget(self, test_engine: AsyncEngine):
        await set_admin_settings_row(test_engine, max_project_spend_total_usd=0.01)
        try:
            yield
        finally:
            await reset_admin_settings_row(test_engine)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [_PRICED_MODEL, _UNPRICED_MODEL])
    async def test_a_count_tokens_lookup_writes_no_row(
        self, test_engine: AsyncEngine, job: UUID, run_for, model: str, counting_budget
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        path = (
            "/projects/proj/locations/us-east5/publishers/anthropic/models/count-tokens:rawPredict"
        )
        body = {"model": model, "input": "hi"}
        plan = _Plan(chunks=[json.dumps({"input_tokens": 12}).encode()])
        app = _proxy_app(_upstream_client({":rawPredict": plan}))
        with TestClient(app) as client:
            response = client.post(path, headers={"x-api-key": placeholder}, json=body)
        assert response.status_code == 200
        assert response.json() == {"input_tokens": 12}
        assert await _rows(test_engine, job) == []

    @pytest.mark.asyncio
    async def test_a_get_answer_writes_no_row(
        self, test_engine: AsyncEngine, job: UUID, run_for
    ) -> None:
        placeholder = run_for(str(job), "discovery").llm_placeholder
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        with TestClient(app) as client:
            client.get("/v1/messages", headers={"x-api-key": placeholder})
        assert await _rows(test_engine, job) == []

    @pytest.mark.asyncio
    async def test_a_dead_run_writes_no_row(self, test_engine: AsyncEngine, job: UUID) -> None:
        body = {"model": _PRICED_MODEL, "messages": []}
        plan = _Plan(chunks=[json.dumps({"usage": _anthropic_usage()}).encode()])
        app = _proxy_app(_upstream_client({"/v1/messages": plan}))
        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages", headers={"x-api-key": "nobody"}, content=json.dumps(body)
            )
        assert resp.status_code == 401
        assert await _rows(test_engine, job) == []


class _ResponsesWebsocketStub:
    """A websocket upstream that, for each received frame, streams its scripted
    replies one after the other and closes after the last, so a turn's frames
    are deterministic: one create drives the whole burst of its events."""

    def __init__(self) -> None:
        self.port = 0
        self.frames: list[bytes] = []
        self.replies: list[bytes] = []
        self.close_after: int | None = None

    async def _handler(self, connection) -> None:
        sent = 0
        while True:
            try:
                message = await connection.recv()
            except ConnectionClosed:
                break
            self.frames.append(message)
            while self.replies:
                await connection.send(self.replies.pop(0))
                sent += 1
                if self.close_after is not None and sent >= self.close_after:
                    await connection.close()
                    return
            if not self.replies:
                await connection.close()
                break


class TestUnreservedMetering:
    async def test_complete_usage_without_reservation_writes_cost(self, test_engine, job):
        from openscientist.llm_metering import _usage_decoder
        from openscientist.llm_proxy import _meter

        decoder = _usage_decoder("/v1/messages", "application/json", _PRICED_MODEL)
        assert decoder is not None
        decoder.feed(json.dumps({"id": "m1", "usage": _anthropic_usage()}).encode())
        await _meter(
            job_id=str(job),
            kind="discovery",
            decoder=decoder,
            provider_id="anthropic",
            status_code=200,
        )("end")
        assert await _rows(test_engine, job) == [
            ("discovery", 3, "anthropic", _PRICED_MODEL, 100, 25, 0, 0, 0, _cost(100, 25))
        ]

    async def test_invalid_job_id_returns_unsaved(self, test_engine, job, caplog):
        from openscientist.agent.base import TokenUsage
        from openscientist.llm_metering import record_model_call

        saved = await record_model_call(
            job_id="invalid",
            kind="discovery",
            model=_PRICED_MODEL,
            provider_id="anthropic",
            buckets=TokenUsage(input_tokens=100, output_tokens=25),
        )
        assert saved is False
        assert "job id is not a UUID" in caplog.text
        assert await _rows(test_engine, job) == []

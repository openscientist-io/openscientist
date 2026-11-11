"""Budget enforcement at the authenticated proxy boundary."""

import asyncio
import json
from contextlib import AsyncExitStack

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from openscientist.llm_proxy import _Admissions, create_llm_proxy_app
from openscientist.providers.base import LlmUpstream, StaticHeadersSigning
from tests import test_llm_proxy as proxy_fixtures
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.test_budget import _cost, committed, job_with_costs
from tests.test_llm_proxy import TestChatgptResponsesWebsocket as WebsocketFixtures
from tests.test_llm_proxy import _recording_upstream

placeholder_for = proxy_fixtures.placeholder_for


@pytest.fixture(autouse=True)
async def reset_limits(test_engine, monkeypatch):
    await set_admin_settings_row(test_engine)
    monkeypatch.setattr("openscientist.budget._provider_billing", _no_billing)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


async def _no_billing():
    return None


@pytest.mark.parametrize(
    "limit", ["max_job_cost_usd", "max_project_spend_total_usd", "max_project_spend_24h_usd"]
)
@pytest.mark.parametrize("spent", [0.5, 1.0])
async def test_http_budget_gate(limit, spent, test_engine, placeholder_for):
    await set_admin_settings_row(test_engine, **{limit: 1.0})
    async with job_with_costs(test_engine, [(spent, None)]) as job:
        recorded = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://upstream.test", StaticHeadersSigning({"x-api-key": "REAL"})
            ),
            client=_recording_upstream(recorded),
            provider_id=lambda: "anthropic",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy"
        ) as client:
            response = await client.post(
                "/v1/messages",
                headers={"x-api-key": placeholder_for(str(job.id))},
                json={"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 10},
            )
        assert response.status_code == (403 if spent == 1.0 else 200)
        assert len(recorded) == (0 if spent == 1.0 else 1)
        if spent == 1.0:
            assert "limit" in response.text


class TestWebsocketBudget:
    stub_upstream = WebsocketFixtures.stub_upstream
    live_proxy = WebsocketFixtures.live_proxy

    async def test_exhausted_handshake_never_reaches_upstream(
        self, test_engine, placeholder_for, live_proxy, stub_upstream
    ):
        await set_admin_settings_row(test_engine, max_job_cost_usd=1.0)
        async with job_with_costs(test_engine, [(1.0, None)]) as job:
            with pytest.raises(InvalidStatus) as raised:
                async with asyncio.timeout(5):
                    await connect(
                        f"ws://127.0.0.1:{live_proxy}/responses",
                        additional_headers={
                            "authorization": f"Bearer {placeholder_for(str(job.id))}"
                        },
                        proxy=None,
                    )
            assert raised.value.response.status_code == 403
            assert stub_upstream.handshakes == []

    @pytest.mark.parametrize("binary", [False, True])
    @pytest.mark.parametrize("model", ["gpt-5.2", None])
    async def test_open_connection_rechecks_each_model_call(
        self, binary, model, test_engine, placeholder_for, live_proxy, stub_upstream
    ):
        await set_admin_settings_row(test_engine, max_job_cost_usd=1.0)
        async with job_with_costs(test_engine, []) as job:
            async with (
                asyncio.timeout(5),
                connect(
                    f"ws://127.0.0.1:{live_proxy}/responses?model=gpt-5.2",
                    additional_headers={"authorization": f"Bearer {placeholder_for(str(job.id))}"},
                    proxy=None,
                ) as connection,
            ):
                frame = {"type": "response.create", "max_output_tokens": 10}
                if model:
                    frame["model"] = model
                text = json.dumps(frame)
                create = text.encode() if binary else text
                await connection.send(create)
                assert await connection.recv() == "stub-reply"
                async with committed(test_engine) as session:
                    await _cost(session, job.id, 1.0)
                await connection.send(create)
                error = json.loads(await connection.recv())
                assert error["type"] == "error"
                assert error["status"] == 403
                assert "Job cost limit" in error["error"]["message"]
            assert stub_upstream.frames == [create]

    @pytest.mark.parametrize(
        "terminal", ["response.completed", "response.failed", "response.incomplete", "error"]
    )
    async def test_terminal_frames_release_admission_for_next_response(
        self, terminal, test_engine, placeholder_for, live_proxy, stub_upstream
    ):
        await set_admin_settings_row(test_engine, max_job_cost_usd=1.0)
        async with job_with_costs(test_engine, []) as job:
            async with (
                asyncio.timeout(10),
                connect(
                    f"ws://127.0.0.1:{live_proxy}/responses",
                    additional_headers={"authorization": f"Bearer {placeholder_for(str(job.id))}"},
                    proxy=None,
                ) as connection,
            ):
                for index in range(6):
                    stream = f"stream-{index % 2}"
                    stub_upstream.reply = json.dumps(
                        {
                            "type": terminal,
                            "stream_id": stream,
                            "response": {
                                "id": f"response-{index}",
                                "model": "gpt-5.2",
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            },
                        }
                    )
                    await connection.send(
                        json.dumps(
                            {
                                "type": "response.create",
                                "model": "gpt-5.2",
                                "stream_id": stream,
                                "max_output_tokens": 10,
                            }
                        )
                    )
                    assert await connection.recv() == stub_upstream.reply
            assert len(stub_upstream.frames) == 6


@pytest.mark.parametrize("aborted", [False, True])
async def test_http_parallel_admission_holds_until_metered(test_engine, placeholder_for, aborted):
    await set_admin_settings_row(test_engine, max_job_cost_usd=1.0)
    entered = asyncio.Queue()
    release = asyncio.Event()

    class HeldBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await release.wait()
            if aborted:
                raise httpx.ReadError("Upstream aborted")
            yield json.dumps({"usage": {"input_tokens": 1_000_000, "output_tokens": 1}}).encode()

    attempts = 0

    async def upstream(request):
        nonlocal attempts
        attempts += 1
        await entered.put(request)
        stream: httpx.AsyncByteStream
        if aborted and attempts > 4:

            class EmptyBody(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"usage": {"input_tokens": 0, "output_tokens": 0}}'

            stream = EmptyBody()
        else:
            stream = HeldBody()
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

    async with job_with_costs(test_engine, []) as job:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
            app = create_llm_proxy_app(
                upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
                client=upstream_client,
                provider_id=lambda: "anthropic",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy"
            ) as client:

                async def call():
                    return await client.post(
                        "/v1/messages",
                        headers={"x-api-key": placeholder_for(str(job.id))},
                        json={"model": "claude-sonnet-4-6", "max_tokens": 10},
                    )

                held = [asyncio.create_task(call()) for _ in range(4)]
                try:
                    async with asyncio.timeout(5):
                        for _ in held:
                            await entered.get()
                    async with asyncio.timeout(1):
                        refused = await call()
                    assert refused.status_code == 429
                    assert entered.empty()
                finally:
                    release.set()
                    async with asyncio.timeout(5):
                        results = await asyncio.gather(*held, return_exceptions=aborted)
                if aborted:
                    assert all(isinstance(result, httpx.ReadError) for result in results)
                assert (await call()).status_code == (200 if aborted else 403)
                assert entered.qsize() == (1 if aborted else 0)


class TestAdmissionWebsocket:
    live_proxy = WebsocketFixtures.live_proxy

    @pytest.fixture
    async def stub_upstream(self):
        class Stub:
            def __init__(self):
                self.release = asyncio.Event()
                self.frames = []
                self.port = 0
                self.abort = False

            async def handler(self, connection):
                async for message in connection:
                    self.frames.append(message)
                    await connection.send("ready")
                    await self.release.wait()
                    if self.abort:
                        await connection.close()
                        return
                    await connection.send(
                        json.dumps(
                            {
                                "type": "response.completed",
                                "response": {
                                    "id": f"r{len(self.frames)}",
                                    "model": "gpt-5.2",
                                    "usage": {"input_tokens": 1_000_000, "output_tokens": 1},
                                },
                            }
                        )
                    )

        stub = Stub()
        async with serve(stub.handler, "127.0.0.1", 0) as server:
            stub.port = server.sockets[0].getsockname()[1]
            yield stub
            stub.release.set()

    @pytest.mark.parametrize("aborted", [False, True])
    async def test_parallel_admission_holds_until_metered(
        self, test_engine, placeholder_for, live_proxy, stub_upstream, aborted
    ):
        await set_admin_settings_row(test_engine, max_job_cost_usd=1.0)
        stub_upstream.abort = aborted
        async with job_with_costs(test_engine, []) as job:
            async with asyncio.timeout(10), AsyncExitStack() as stack:
                connections = [
                    await stack.enter_async_context(
                        connect(
                            f"ws://127.0.0.1:{live_proxy}/responses",
                            additional_headers={
                                "authorization": f"Bearer {placeholder_for(str(job.id))}"
                            },
                            proxy=None,
                        )
                    )
                    for _ in range(5)
                ]
                create = json.dumps(
                    {"type": "response.create", "model": "gpt-5.2", "max_output_tokens": 10}
                )
                try:
                    for connection in connections[:4]:
                        await connection.send(create)
                        assert await connection.recv() == "ready"
                    await connections[4].send(create)
                    refusal = await connections[4].recv()
                    assert json.loads(refusal)["status"] == 429
                    assert len(stub_upstream.frames) == 4
                finally:
                    stub_upstream.release.set()
                if aborted:
                    for connection in connections[:4]:
                        with pytest.raises(ConnectionClosed):
                            await connection.recv()
                    from sqlalchemy import func, select

                    from openscientist.database.models import CostRecord

                    while True:
                        async with committed(test_engine) as session:
                            count = await session.scalar(
                                select(func.count())
                                .select_from(CostRecord)
                                .where(CostRecord.job_id == job.id)
                            )
                        if count == 4:
                            break
                        await asyncio.sleep(0.01)
                    async with connect(
                        f"ws://127.0.0.1:{live_proxy}/responses",
                        additional_headers={
                            "authorization": f"Bearer {placeholder_for(str(job.id))}"
                        },
                        proxy=None,
                    ) as next_connection:
                        await next_connection.send(create)
                        assert await next_connection.recv() == "ready"
                    assert len(stub_upstream.frames) == 5
                else:
                    for connection in connections[:4]:
                        assert json.loads(await connection.recv())["type"] == "response.completed"
                    await connections[0].send(create)
                    assert json.loads(await connections[0].recv())["status"] == 403
                    assert len(stub_upstream.frames) == 4

    async def test_aborted_connections_exhaust_reserved_budget(
        self, test_engine, placeholder_for, live_proxy, stub_upstream
    ):
        await set_admin_settings_row(test_engine, max_job_cost_usd=0.001)
        stub_upstream.abort = True
        stub_upstream.release.set()
        async with job_with_costs(test_engine, []) as job:
            async with asyncio.timeout(15):
                for _ in range(10):
                    try:
                        async with connect(
                            f"ws://127.0.0.1:{live_proxy}/responses",
                            additional_headers={
                                "authorization": f"Bearer {placeholder_for(str(job.id))}"
                            },
                            proxy=None,
                        ) as connection:
                            await connection.send(
                                json.dumps(
                                    {
                                        "type": "response.create",
                                        "model": "gpt-5.2",
                                        "max_output_tokens": 10,
                                    }
                                )
                            )
                            response = await connection.recv()
                            if response != "ready":
                                assert json.loads(response)["status"] == 403
                                break
                            with pytest.raises(ConnectionClosed):
                                await connection.recv()
                    except InvalidStatus as exc:
                        assert exc.response.status_code == 403
                        break
                else:
                    pytest.fail("Aborted websocket calls never exhausted the job budget")
        assert 0 < len(stub_upstream.frames) < 10


async def test_cancelling_meter_wait_keeps_slots_until_settlement():
    admissions = _Admissions()
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def meter():
        await entered.put(None)
        await release.wait()

    held = [admissions.acquire("job") for _ in range(4)]
    tasks = []
    try:
        for admission in held:
            assert admission is not None
            tasks.append(asyncio.create_task(admissions.settle(admission, meter())))
        async with asyncio.timeout(5):
            for _ in held:
                await entered.get()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert admissions.acquire("job") is None
        assert admissions.active == {"job": 4}
    finally:
        release.set()
        async with asyncio.timeout(5):
            await asyncio.gather(*admissions.settlements)
    assert admissions.active == {}
    next_call = admissions.acquire("job")
    assert next_call is not None
    next_call.release()


async def test_failed_meter_releases_admission():
    admissions = _Admissions()

    async def meter():
        raise RuntimeError("Ledger unavailable")

    for _ in range(5):
        admission = admissions.acquire("job")
        assert admission is not None
        async with asyncio.timeout(5):
            await admissions.settle(admission, meter())
        assert admissions.active == {}


async def test_http_cancellation_before_headers_releases_admission(
    test_engine, placeholder_for, caplog
):
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def upstream(request):
        await entered.put(None)
        await release.wait()
        raise httpx.ConnectError("Upstream unavailable")

    async with job_with_costs(test_engine, []) as job:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
            app = create_llm_proxy_app(
                upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
                client=upstream_client,
                provider_id=lambda: "anthropic",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy"
            ) as client:

                async def call():
                    return await client.post(
                        "/v1/messages",
                        headers={"x-api-key": placeholder_for(str(job.id))},
                        json={"model": "claude-sonnet-4-6"},
                    )

                held = [asyncio.create_task(call()) for _ in range(4)]
                try:
                    async with asyncio.timeout(5):
                        for _ in held:
                            await entered.get()
                    assert (await call()).status_code == 429
                finally:
                    for task in held:
                        task.cancel()
                    async with asyncio.timeout(5):
                        await asyncio.gather(*held, return_exceptions=True)
                release.set()
                async with asyncio.timeout(5):
                    with pytest.raises(httpx.ConnectError):
                        await call()
                assert entered.qsize() == 1
                assert caplog.text.count("ended before response headers") == 5


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/messages"),
        ("POST", "/v1/messages/count_tokens"),
        ("GET", "/v1/models"),
        ("HEAD", "/api/hello"),
    ],
)
async def test_admission_precedes_every_body_read(method, path, test_engine, placeholder_for):
    async with job_with_costs(test_engine, []) as job:
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
            client=_recording_upstream([]),
            provider_id=lambda: "anthropic",
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"x-api-key", placeholder_for(str(job.id)).encode())],
        }
        entered = asyncio.Queue()
        blocked = asyncio.Event()
        replies = []

        async def receive():
            await entered.put(None)
            await blocked.wait()
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            replies.append(message)

        async def forbidden_receive():
            pytest.fail("Refused request body was read")

        held = [asyncio.create_task(app(scope, receive, send)) for _ in range(4)]
        try:
            async with asyncio.timeout(5):
                for _ in held:
                    await entered.get()
                await app(scope, forbidden_receive, send)
                assert replies[0]["status"] == 429
                held[0].cancel()
                await asyncio.gather(held[0], return_exceptions=True)
                replacement = asyncio.create_task(app(scope, receive, send))
                held.append(replacement)
                await entered.get()
                assert not replacement.done()
        finally:
            for task in held:
                task.cancel()
            async with asyncio.timeout(5):
                await asyncio.gather(*held, return_exceptions=True)


@pytest.mark.parametrize("ending", ["before_headers", "without_usage", "partial_usage"])
async def test_aborted_http_calls_exhaust_reserved_budget(ending, test_engine, placeholder_for):
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.001)
    attempts = 0

    class AbortedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            if ending == "partial_usage":
                yield (
                    b'event: message_start\ndata: {"type":"message_start","message":'
                    b'{"model":"claude-sonnet-4-6","usage":{"input_tokens":1}}}\n\n'
                )
            raise httpx.ReadError("Upstream aborted")

    async def upstream(request):
        nonlocal attempts
        attempts += 1
        if ending == "before_headers":
            raise httpx.ReadError("Upstream aborted")
        return httpx.Response(
            200, stream=AbortedBody(), headers={"content-type": "text/event-stream"}
        )

    async with job_with_costs(test_engine, []) as job:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
            app = create_llm_proxy_app(
                upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
                client=upstream_client,
                provider_id=lambda: "anthropic",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy"
            ) as client:
                for _ in range(10):
                    try:
                        response = await client.post(
                            "/v1/messages",
                            headers={"x-api-key": placeholder_for(str(job.id))},
                            json={"model": "claude-sonnet-4-6", "max_tokens": 10},
                        )
                    except httpx.TransportError:
                        continue
                    assert response.status_code == 403
                    break
                else:
                    pytest.fail("Aborted model calls never exhausted the job budget")
    assert 0 < attempts < 10


@pytest.mark.parametrize(
    "failure,charged",
    [
        (httpx.ConnectError, False),
        (httpx.ConnectTimeout, False),
        (httpx.PoolTimeout, False),
        (httpx.ReadTimeout, True),
        (httpx.WriteTimeout, True),
    ],
)
async def test_transport_failure_settlement(failure, charged, test_engine, placeholder_for):
    from tests.test_llm_reservations import rows

    async def upstream(request):
        raise failure("Upstream unavailable")

    async with job_with_costs(test_engine, []) as job:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
            app = create_llm_proxy_app(
                upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
                client=upstream_client,
                provider_id=lambda: "anthropic",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy"
            ) as client:
                with pytest.raises(failure):
                    await client.post(
                        "/v1/messages",
                        headers={"x-api-key": placeholder_for(str(job.id))},
                        json={"model": "claude-sonnet-4-6", "max_tokens": 10},
                    )
            recorded = await rows(test_engine, job.id)
            assert len(recorded) == int(charged)
            if recorded:
                assert recorded[0].cost_usd > 0
                assert recorded[0].operation_type.endswith("_estimated")


@pytest.mark.parametrize(
    "provider,status,code,charged",
    [
        *[
            (provider, status, None, False)
            for provider in ("anthropic", "vertex")
            for status in (400, 401, 403, 404, 429, 500, 502, 503, 504, 529)
        ],
        *[("bedrock", status, None, False) for status in (400, 401, 404, 429)],
        *[("bedrock", status, None, True) for status in (403, 500, 502, 503, 504, 529)],
        *[("openai", status, None, False) for status in (400, 401, 403, 404, 429)],
        *[("openai", status, None, True) for status in (500, 502, 503, 504)],
        ("openai", 503, "server_is_overloaded", False),
    ],
)
async def test_error_status_settlement(
    provider, status, code, charged, test_engine, placeholder_for, monkeypatch
):
    from openscientist.settings import get_settings
    from tests.test_llm_reservations import rows

    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
    get_settings.cache_clear()
    await set_admin_settings_row(test_engine, max_job_cost_usd=0.001)
    paths = {
        "anthropic": "/v1/messages",
        "vertex": "/projects/proj/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-6:streamRawPredict",
        "bedrock": "/model/claude-sonnet-4-6/invoke-with-response-stream",
        "openai": "/responses",
    }

    async def upstream(request):
        return httpx.Response(
            status,
            stream=httpx.ByteStream(
                json.dumps({"error": {"code": code, "message": "Unavailable"}}).encode()
            ),
            headers={"content-type": "application/json"},
        )

    try:
        async with job_with_costs(test_engine, []) as job:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(upstream)
            ) as upstream_client:
                app = create_llm_proxy_app(
                    upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
                    client=upstream_client,
                    provider_id=lambda: provider,
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://proxy"
                ) as client:
                    for _ in range(1 if charged else 10):
                        response = await client.post(
                            paths[provider],
                            headers={"x-api-key": placeholder_for(str(job.id))},
                            json={"model": "claude-sonnet-4-6", "max_tokens": 10},
                        )
                        assert response.status_code == status
                recorded = await rows(test_engine, job.id)
                assert len(recorded) == int(charged)
                if recorded:
                    assert recorded[0].cost_usd > 0
                    assert recorded[0].operation_type.endswith("_estimated")
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "provider,path,usage",
    [
        ("anthropic", "/v1/messages", {"input_tokens": 1000, "output_tokens": 1000}),
        ("openai", "/responses", {"input_tokens": 1000, "output_tokens": 1000}),
        ("openai", "/responses/compact", {"input_tokens": 1000, "output_tokens": 1000}),
        ("ollama", "/chat/completions", {"prompt_tokens": 1000, "completion_tokens": 1000}),
    ],
)
async def test_error_response_with_usage_retains_estimate(
    provider, path, usage, test_engine, placeholder_for, monkeypatch
):
    from tests.test_llm_reservations import rows

    monkeypatch.setattr(
        "openscientist.providers.pricing._get_litellm_pricing",
        lambda: {
            "claude-sonnet-4-6": {
                "input_cost_per_token": 0.001,
                "output_cost_per_token": 0.002,
            }
        },
    )

    async def upstream(request):
        return httpx.Response(
            500,
            stream=httpx.ByteStream(json.dumps({"usage": usage}).encode()),
            headers={"content-type": "application/json"},
        )

    async with job_with_costs(test_engine, []) as job:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
            app = create_llm_proxy_app(
                upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
                client=upstream_client,
                provider_id=lambda: provider,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy"
            ) as client:
                response = await client.post(
                    path,
                    headers={"x-api-key": placeholder_for(str(job.id))},
                    json={"model": "claude-sonnet-4-6", "max_tokens": 10},
                )
                assert response.status_code == 500
            recorded = await rows(test_engine, job.id)
            assert len(recorded) == 1
            assert recorded[0].cost_usd == pytest.approx(3.0)
            assert recorded[0].operation_type.endswith("_estimated")

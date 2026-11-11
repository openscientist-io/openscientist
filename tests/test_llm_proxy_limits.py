"""Bounded request buffering at the authenticated LLM proxy."""

import asyncio

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from openscientist import llm_proxy
from openscientist.providers.base import LlmUpstream, StaticHeadersSigning
from tests import test_llm_proxy as proxy_fixtures

placeholder_for = proxy_fixtures.placeholder_for


@pytest.fixture(autouse=True)
def small_request_limit(monkeypatch):
    monkeypatch.setattr(llm_proxy, "_MAX_REQUEST_BYTES", 1024, raising=False)


@pytest.mark.parametrize("declared_length", ["1025", "9" * 5000])
async def test_oversized_content_length_is_refused_without_reading(
    declared_length, placeholder_for
):
    recorded = []
    read_chunks = 0

    async def body():
        nonlocal read_chunks
        read_chunks += 1
        yield b"{}"

    app = llm_proxy.create_llm_proxy_app(
        upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
        client=proxy_fixtures._recording_upstream(recorded),
        provider_id=lambda: "anthropic",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/messages",
            headers={"x-api-key": placeholder_for("run"), "content-length": declared_length},
            content=body(),
        )
    assert response.status_code == 413
    assert read_chunks == 0
    assert recorded == []


async def test_chunked_body_stops_reading_at_limit(placeholder_for):
    recorded = []
    read_chunks = 0

    async def body():
        nonlocal read_chunks
        for _ in range(4):
            read_chunks += 1
            yield b" " * 512

    app = llm_proxy.create_llm_proxy_app(
        upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
        client=proxy_fixtures._recording_upstream(recorded),
        provider_id=lambda: "anthropic",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/messages", headers={"x-api-key": placeholder_for("run")}, content=body()
        )
    assert response.status_code == 413
    assert read_chunks == 3
    assert recorded == []


@pytest.mark.parametrize("streaming", [False, True])
async def test_request_at_limit_is_forwarded(streaming, placeholder_for):
    recorded = []
    payload = b" " * 1022 + b"{}"

    async def body():
        yield payload[:512]
        yield payload[512:]

    app = llm_proxy.create_llm_proxy_app(
        upstream=lambda: LlmUpstream("https://upstream.test", StaticHeadersSigning({})),
        client=proxy_fixtures._recording_upstream(recorded),
        provider_id=lambda: "anthropic",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/messages",
            headers={"x-api-key": placeholder_for("run")},
            content=body() if streaming else payload,
        )
    assert response.status_code == 200
    assert recorded[0]["body"] == payload


class TestWebsocketRequestLimits:
    stub_upstream = proxy_fixtures.TestChatgptResponsesWebsocket.stub_upstream
    live_proxy = proxy_fixtures.TestChatgptResponsesWebsocket.live_proxy

    @pytest.mark.parametrize("payload", ["x" * 1025, "\u00e9" * 513, b"x" * 1025])
    async def test_oversized_message_never_reaches_upstream(
        self, payload, live_proxy, stub_upstream, placeholder_for
    ):
        async with (
            asyncio.timeout(5),
            connect(
                f"ws://127.0.0.1:{live_proxy}/responses",
                additional_headers={"authorization": f"Bearer {placeholder_for('run')}"},
                proxy=None,
            ) as connection,
        ):
            await connection.send(payload)
            with pytest.raises(ConnectionClosed) as raised:
                await connection.recv()
            assert raised.value.rcvd is not None
            assert raised.value.rcvd.code == 1009
        assert stub_upstream.frames == []

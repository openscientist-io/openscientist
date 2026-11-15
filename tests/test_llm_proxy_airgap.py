"""An air-gapped run reaches only the LLM proxy, so the provider is the one
party that could still fetch from the internet for it. The proxy forwards
from an air-gapped run only requests whose tools the harness runs itself."""

import asyncio
import gzip
import json
import socket
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from starlette.testclient import TestClient
from uvicorn.config import Config
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from openscientist.job_container.secrets import issue_run_credentials, revoke_run_credentials
from openscientist.llm_proxy import _NoSignalServer, create_llm_proxy_app
from openscientist.providers.base import LlmUpstream, StaticHeadersSigning
from tests.test_budget import job_with_costs
from tests.test_llm_proxy import _recording_upstream

IssuePlaceholder = Callable[..., str]

_ATTACKER = "https://attacker.example/mcp"

# Shapes the harnesses send, reduced from requests captured from the pinned
# Claude Code, codex and omp against a fake provider.
_CLAUDE_CODE_REQUEST = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 32000,
    "stream": True,
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    "tools": [
        {"name": "Read", "description": "Read a file.", "input_schema": {"type": "object"}},
        {
            "name": "mcp__openscientist-tools__execute_code",
            "description": "Run code.",
            "input_schema": {"type": "object"},
        },
    ],
    "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
    "thinking": {"type": "adaptive"},
    "metadata": {"user_id": "u"},
}
_CODEX_REQUEST = {
    "model": "gpt-5.5",
    "stream": True,
    "store": False,
    "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ],
    "tools": [
        {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
        {"type": "custom", "name": "apply_patch", "format": {"type": "grammar"}},
        {
            "type": "namespace",
            "name": "multi_agent_v1",
            "tools": [{"type": "function", "name": "spawn_agent", "parameters": {}}],
        },
        {"type": "tool_search", "execution": "client", "parameters": {"type": "object"}},
    ],
}
_CODEX_LOADED_TOOLS_REQUEST = {
    **_CODEX_REQUEST,
    "input": [
        {
            "type": "tool_search_output",
            "call_id": "call_1",
            "execution": "client",
            "status": "completed",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__openscientist_tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "execute_code",
                            "defer_loading": True,
                            "parameters": {},
                        }
                    ],
                }
            ],
        }
    ],
}
_OMP_CHAT_REQUEST = {
    "model": "local-model",
    "stream": True,
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}],
}

_HARNESS_REQUESTS = [
    pytest.param("anthropic", "/v1/messages", _CLAUDE_CODE_REQUEST, id="claude-code"),
    pytest.param("anthropic", "/v1/messages/count_tokens", _CLAUDE_CODE_REQUEST, id="count"),
    pytest.param("foundry", "/v1/messages", _CLAUDE_CODE_REQUEST, id="foundry"),
    pytest.param("openai", "/responses", _CODEX_REQUEST, id="codex"),
    pytest.param("openai", "/responses", _CODEX_LOADED_TOOLS_REQUEST, id="codex-loaded"),
    pytest.param("azure-openai", "/responses/compact", _CODEX_REQUEST, id="codex-compact"),
    pytest.param("vllm", "/chat/completions", _OMP_CHAT_REQUEST, id="omp-chat"),
]


def _with(base: dict, **fields: object) -> dict:
    return {**base, **fields}


_REMOTE_REQUESTS = [
    pytest.param(
        "anthropic",
        "/v1/messages",
        _with(
            _CLAUDE_CODE_REQUEST,
            mcp_servers=[{"type": "url", "url": _ATTACKER, "name": "x"}],
        ),
        id="anthropic-mcp-servers",
    ),
    pytest.param(
        "anthropic",
        "/v1/messages",
        _with(_CLAUDE_CODE_REQUEST, tools=[{"type": "web_fetch_20250910", "name": "web_fetch"}]),
        id="anthropic-web-fetch",
    ),
    pytest.param(
        "cborg",
        "/v1/messages",
        _with(
            _CLAUDE_CODE_REQUEST,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        ),
        id="claude-code-websearch",
    ),
    pytest.param(
        "foundry",
        "/v1/messages",
        _with(_CLAUDE_CODE_REQUEST, container={"skills": [{"type": "anthropic"}]}),
        id="anthropic-container",
    ),
    pytest.param(
        "bedrock",
        "/model/anthropic.claude-sonnet-4-6/invoke",
        _with(
            _CLAUDE_CODE_REQUEST,
            tools=[{"type": "mcp_toolset", "mcp_server_name": "x"}],
        ),
        id="bedrock-mcp-toolset",
    ),
    pytest.param(
        "openai",
        "/responses",
        _with(_CODEX_REQUEST, tools=[{"type": "mcp", "server_url": _ATTACKER}]),
        id="responses-mcp",
    ),
    pytest.param(
        "openai",
        "/responses",
        _with(_CODEX_REQUEST, tools=[{"type": "web_search", "external_web_access": False}]),
        id="responses-cached-search",
    ),
    pytest.param(
        "azure-openai",
        "/responses",
        _with(_CODEX_REQUEST, tools=[{"type": "tool_search"}]),
        id="responses-hosted-tool-search",
    ),
    pytest.param(
        "openai",
        "/responses",
        _with(
            _CODEX_REQUEST,
            tools=[{"type": "namespace", "name": "n", "tools": [{"type": "web_search"}]}],
        ),
        id="responses-namespace-search",
    ),
    pytest.param(
        "openai",
        "/responses",
        _with(
            _CODEX_LOADED_TOOLS_REQUEST,
            input=[
                {
                    "type": "tool_search_output",
                    "execution": "client",
                    "tools": [{"type": "mcp", "server_url": _ATTACKER, "defer_loading": True}],
                }
            ],
        ),
        id="responses-loaded-mcp",
    ),
    pytest.param(
        "openai",
        "/responses",
        _with(_CODEX_REQUEST, previous_response_id="resp_elsewhere"),
        id="responses-previous-response",
    ),
    pytest.param(
        "openai",
        "/responses",
        _with(_CODEX_REQUEST, conversation="conv_elsewhere"),
        id="responses-conversation",
    ),
    pytest.param(
        "openai",
        "/responses",
        _with(_CODEX_REQUEST, prompt={"id": "pmpt_elsewhere"}),
        id="responses-stored-prompt",
    ),
    pytest.param(
        "llamacpp",
        "/chat/completions",
        _with(_OMP_CHAT_REQUEST, web_search_options={}),
        id="chat-web-search",
    ),
    pytest.param(
        "ollama",
        "/chat/completions",
        _with(_OMP_CHAT_REQUEST, tools=[{"type": "web_search"}]),
        id="chat-server-tool",
    ),
]


@pytest_asyncio.fixture
async def placeholder(test_engine) -> AsyncIterator[IssuePlaceholder]:
    """Issue a placeholder for a committed job's run, air-gapped or not, ending
    each run after the test."""
    roles: list[str] = []
    async with job_with_costs(test_engine, []) as job:

        def issue(*, airgapped: bool) -> str:
            role = f"agent_{uuid4().hex}"
            roles.append(role)
            credentials = issue_run_credentials(str(job.id), role, "discovery", airgapped=airgapped)
            return credentials.llm_placeholder

        yield issue
        for role in roles:
            revoke_run_credentials(role)


def _post(
    provider: str,
    path: str,
    content: bytes,
    placeholder: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, list[dict]]:
    """The proxy's status and text for one POST, and the requests it forwarded."""
    recorded: list[dict] = []
    app = create_llm_proxy_app(
        upstream=lambda: LlmUpstream(
            "https://upstream.test", StaticHeadersSigning({"x-api-key": "REAL"})
        ),
        client=_recording_upstream(recorded),
        provider_id=lambda: provider,
    )
    with TestClient(app) as client:
        response = client.post(
            path,
            headers={
                "x-api-key": placeholder,
                "content-type": "application/json",
                **(headers or {}),
            },
            content=content,
        )
    return response.status_code, response.text, recorded


class TestAirgappedHttp:
    @pytest.mark.parametrize(("provider", "path", "request_body"), _HARNESS_REQUESTS)
    def test_harness_requests_are_forwarded_unchanged(
        self, provider, path, request_body, placeholder
    ):
        content = json.dumps(request_body).encode()
        status, _, recorded = _post(provider, path, content, placeholder(airgapped=True))
        assert status == 200
        assert [call["body"] for call in recorded] == [content]

    @pytest.mark.parametrize(("provider", "path", "request_body"), _REMOTE_REQUESTS)
    def test_requests_reaching_past_the_harness_are_refused(
        self, provider, path, request_body, placeholder
    ):
        content = json.dumps(request_body).encode()
        status, text, recorded = _post(provider, path, content, placeholder(airgapped=True))
        assert status == 403
        assert "air-gapped" in text
        assert recorded == []

    @pytest.mark.parametrize(("provider", "path", "request_body"), _REMOTE_REQUESTS)
    def test_runs_that_are_not_air_gapped_are_forwarded_unchanged(
        self, provider, path, request_body, placeholder
    ):
        content = json.dumps(request_body).encode()
        status, _, recorded = _post(provider, path, content, placeholder(airgapped=False))
        assert status == 200
        assert [call["body"] for call in recorded] == [content]

    @pytest.mark.parametrize(
        ("content", "headers"),
        [
            pytest.param(
                b'{"model": "gpt-5.5", "tools": [], "tools": [{"type": "mcp"}]}',
                {},
                id="repeated-key",
            ),
            pytest.param(
                b'{"model": "gpt-5.5", "input": [{"type": "x", "tools": [], "tools": [{}]}]}',
                {},
                id="nested-repeated-key",
            ),
            pytest.param(b"not json", {}, id="not-json"),
            pytest.param(b"[]", {}, id="not-an-object"),
            pytest.param(b'{"model": "gpt-5.5", "temperature": NaN}', {}, id="nan"),
            pytest.param(
                gzip.compress(json.dumps(_CODEX_REQUEST).encode()),
                {"content-encoding": "gzip"},
                id="compressed",
            ),
        ],
    )
    def test_bodies_the_proxy_cannot_read_as_the_provider_does_are_refused(
        self, content, headers, placeholder
    ):
        status, _, recorded = _post(
            "openai", "/responses", content, placeholder(airgapped=True), headers
        )
        assert status == 403
        assert recorded == []

    def test_a_bodiless_model_listing_is_forwarded(self, placeholder):
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://upstream.test", StaticHeadersSigning({"x-api-key": "REAL"})
            ),
            client=_recording_upstream(recorded),
            provider_id=lambda: "anthropic",
        )
        with TestClient(app) as client:
            response = client.get("/v1/models", headers={"x-api-key": placeholder(airgapped=True)})
        assert response.status_code == 200
        assert len(recorded) == 1


class _ResponsesUpstream:
    """A websocket Responses upstream that records each frame and answers it
    with a created and a completed response carrying a fresh id."""

    def __init__(self) -> None:
        self.port = 0
        self.frames: list[dict] = []

    async def handler(self, connection) -> None:
        try:
            async for message in connection:
                frame = json.loads(message)
                self.frames.append(frame)
                response = {
                    "id": f"resp_{len(self.frames)}",
                    "object": "response",
                    "model": frame.get("model", "gpt-5.5"),
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                await connection.send(
                    json.dumps({"type": "response.created", "response": response})
                )
                await connection.send(
                    json.dumps({"type": "response.completed", "response": response})
                )
        except ConnectionClosed:
            return


@pytest.fixture
async def responses_upstream() -> AsyncIterator[_ResponsesUpstream]:
    upstream = _ResponsesUpstream()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        upstream.port = probe.getsockname()[1]
    async with serve(upstream.handler, "127.0.0.1", upstream.port):
        yield upstream


@pytest.fixture
async def live_proxy(responses_upstream) -> AsyncIterator[int]:
    """The real proxy on a real local port, in front of the Responses upstream."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(None))
    app = create_llm_proxy_app(
        upstream=lambda: LlmUpstream(
            f"http://127.0.0.1:{responses_upstream.port}",
            StaticHeadersSigning({"authorization": "Bearer REAL"}),
        ),
        client=client,
        provider_id=lambda: "openai",
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
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
        with pytest.raises(asyncio.CancelledError):
            await task
        sock.close()
        await client.aclose()


async def _send_frames(port: int, placeholder: str, frames: list[dict]) -> list[dict]:
    """Send each frame and read until its response completes or the proxy
    refuses, returning every frame received."""
    received: list[dict] = []
    async with connect(
        f"ws://127.0.0.1:{port}/responses",
        additional_headers={"authorization": f"Bearer {placeholder}"},
        proxy=None,
    ) as connection:
        for frame in frames:
            await connection.send(json.dumps(frame))
            while True:
                try:
                    message = json.loads(await connection.recv())
                except ConnectionClosed:
                    return received
                received.append(message)
                if message.get("type") in {"response.completed", "error"}:
                    break
    return received


def _create(**fields: object) -> dict:
    return {"type": "response.create", **_CODEX_REQUEST, **fields}


class TestAirgappedWebsocket:
    async def test_codex_chaining_on_one_connection_is_forwarded(
        self, responses_upstream, live_proxy, placeholder
    ):
        frames = [
            _create(generate=False),
            _create(previous_response_id="resp_1"),
            _create(previous_response_id="resp_2", input=_CODEX_LOADED_TOOLS_REQUEST["input"]),
        ]
        received = await _send_frames(live_proxy, placeholder(airgapped=True), frames)
        assert [message["type"] for message in received].count("response.completed") == 3
        assert responses_upstream.frames == frames

    @pytest.mark.parametrize(
        "frame",
        [
            pytest.param(_create(tools=[{"type": "mcp", "server_url": _ATTACKER}]), id="mcp"),
            pytest.param(
                _create(
                    input=[
                        {
                            "type": "tool_search_output",
                            "execution": "client",
                            "tools": [{"type": "mcp", "server_url": _ATTACKER}],
                        }
                    ]
                ),
                id="loaded-mcp",
            ),
            pytest.param(_create(tools=[{"type": "web_search"}]), id="search"),
            pytest.param(_create(previous_response_id="resp_1"), id="unseen-previous"),
        ],
    )
    async def test_frames_reaching_past_the_harness_are_refused(
        self, frame, responses_upstream, live_proxy, placeholder
    ):
        received = await _send_frames(live_proxy, placeholder(airgapped=True), [frame])
        assert [message["type"] for message in received] == ["error"]
        assert "air-gapped" in received[0]["error"]["message"]
        assert responses_upstream.frames == []

    async def test_an_id_from_another_connection_is_refused(
        self, responses_upstream, live_proxy, placeholder
    ):
        issued = placeholder(airgapped=True)
        await _send_frames(live_proxy, issued, [_create()])
        received = await _send_frames(live_proxy, issued, [_create(previous_response_id="resp_1")])
        assert [message["type"] for message in received] == ["error"]
        assert len(responses_upstream.frames) == 1

    async def test_runs_that_are_not_air_gapped_are_forwarded_unchanged(
        self, responses_upstream, live_proxy, placeholder
    ):
        frame = _create(tools=[{"type": "mcp", "server_url": _ATTACKER}])
        received = await _send_frames(live_proxy, placeholder(airgapped=False), [frame])
        assert received[-1]["type"] == "response.completed"
        assert responses_upstream.frames == [frame]

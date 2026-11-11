"""Broker authentication precedes bounded request body reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from starlette.types import Message, Scope

from openscientist.exec_broker import create_exec_broker_app
from openscientist.exec_broker_client import EXEC_TOKEN_HEADER
from openscientist.job_container.secrets import issue_run_credentials, revoke_run_credentials

_ROUTES = [("/execute", 1024 * 1024), ("/budget", 4096)]


@pytest.fixture
def token() -> Iterator[str]:
    role = f"agent_{uuid4().hex}"
    yield issue_run_credentials("job-1", role, "discovery").exec_token
    revoke_run_credentials(role)


async def _post(
    path: str,
    *,
    token: str | None,
    chunks: list[bytes],
    content_length: str | None = None,
) -> tuple[int, int]:
    headers = []
    if token is not None:
        headers.append((EXEC_TOKEN_HEADER.encode(), token.encode()))
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
    }
    reads = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal reads
        chunk = chunks[reads]
        reads += 1
        return {"type": "http.request", "body": chunk, "more_body": reads < len(chunks)}

    async def send(message: Message) -> None:
        messages.append(message)

    manager = MagicMock()
    app = create_exec_broker_app(manager=lambda: manager)
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    manager.execute_code.assert_not_called()
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    return status, reads


@pytest.mark.parametrize(("path", "limit"), _ROUTES)
async def test_oversized_content_length_is_rejected_without_reading(
    path: str, limit: int, token: str
) -> None:
    status, reads = await _post(path, token=token, chunks=[b"{}"], content_length=str(limit + 1))
    assert status == 413
    assert reads == 0


@pytest.mark.parametrize(("path", "limit"), _ROUTES)
@pytest.mark.parametrize("content_length", [None, "1"])
async def test_streaming_overflow_stops_at_the_limit(
    path: str, limit: int, token: str, content_length: str | None
) -> None:
    status, reads = await _post(
        path,
        token=token,
        chunks=[b" " * limit, b"x", b"unread"],
        content_length=content_length,
    )
    assert status == 413
    assert reads == 2


@pytest.mark.parametrize("path", ["/execute", "/budget"])
@pytest.mark.parametrize("credential", [None, "invalid", "job-1.forged"])
async def test_unauthenticated_request_never_reads_body(path: str, credential: str | None) -> None:
    status, reads = await _post(
        path,
        token=credential,
        chunks=[b'{"job_id":"job-1","request_id":"req-1"}'],
    )
    assert status == 401
    assert reads == 0


@pytest.mark.parametrize("path", ["/execute", "/budget"])
async def test_revoked_token_never_reads_body(path: str) -> None:
    role = f"agent_{uuid4().hex}"
    credential = issue_run_credentials("job-1", role, "discovery").exec_token
    revoke_run_credentials(role)
    status, reads = await _post(
        path, token=credential, chunks=[b'{"job_id":"job-1","request_id":"req-1"}']
    )
    assert status == 401
    assert reads == 0


@pytest.mark.parametrize("path", ["/execute", "/budget"])
@pytest.mark.parametrize("content_length", ["-1", "invalid", "+1", "1, 1"])
async def test_invalid_content_length_is_rejected_without_reading(
    path: str, token: str, content_length: str
) -> None:
    status, reads = await _post(path, token=token, chunks=[b"{}"], content_length=content_length)
    assert status == 400
    assert reads == 0


@pytest.mark.parametrize(("path", "limit"), _ROUTES)
async def test_body_at_limit_reaches_job_binding(path: str, limit: int, token: str) -> None:
    payload = json.dumps({"job_id": "other-job", "request_id": "req-1"}).encode()
    status, reads = await _post(
        path, token=token, chunks=[payload.ljust(limit, b" ")], content_length=str(limit)
    )
    assert status == 401
    assert reads == 1


@pytest.mark.parametrize("path", ["/execute", "/budget"])
@pytest.mark.parametrize("payload", [b"not json", b"[]", b"\xff"])
async def test_invalid_json_is_rejected(path: str, token: str, payload: bytes) -> None:
    status, reads = await _post(path, token=token, chunks=[payload])
    assert status == 400
    assert reads == 1

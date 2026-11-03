"""The container side of the budget check: one ask per unit of work, each request
carrying a fresh id the reply must quote, retries with backoff inside the window,
and a stop with a note when the budget could not be checked."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import httpx
import pytest

from openscientist.exec_broker_client import (
    EXEC_BROKER_URL_ENV,
    EXEC_TOKEN_ENV,
    EXEC_TOKEN_HEADER,
    ask_budget,
)

ALLOWED: dict[str, object] = {"ok": True, "limit": None, "message": None}
JOB_CAP: dict[str, object] = {
    "ok": False,
    "limit": "job_cap",
    "message": "Job cost limit of $5.00 reached: the job has spent $5.00.",
}


class _Scripted(httpx.AsyncBaseTransport):
    """The network boundary between the container and the broker.

    Each request gets the next scripted reply, the last one repeating. A reply
    is a status code, a raw 200 body, a verdict dict, a callable building one,
    or an exception class the transport raises."""

    def __init__(self, replies: list[object]) -> None:
        self._replies = replies
        self.requests: list[httpx.Request] = []

    def _reply(self, request: httpx.Request) -> httpx.Response:
        scripted = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        if isinstance(scripted, type) and issubclass(scripted, BaseException):
            raise scripted("connection refused")
        if callable(scripted):
            scripted = scripted(request)
        if isinstance(scripted, int):
            return httpx.Response(scripted, request=request, content=b"no verdict")
        if isinstance(scripted, str):
            return httpx.Response(200, request=request, content=scripted.encode())
        return httpx.Response(200, request=request, content=json.dumps(scripted).encode())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._reply(request)


def _verdict(reply: dict[str, object]) -> Callable[[httpx.Request], dict[str, object]]:
    """A broker reply that quotes the request it answers."""

    def make(request: httpx.Request) -> dict[str, object]:
        body = json.loads(request.content)
        return {**reply, "request_id": body["request_id"]}

    return make


def _clock_and_sleep() -> tuple[Callable[[], float], Callable[[float], Awaitable[None]]]:
    """A controllable clock, and a sleep that advances it by the asked delay."""
    state = {"now": 0.0}

    def clock() -> float:
        return state["now"]

    async def sleep(delay: float) -> None:
        state["now"] += delay

    return clock, sleep


@pytest.fixture(autouse=True)
def broker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXEC_TOKEN_ENV, "job-1.token")
    monkeypatch.setenv(EXEC_BROKER_URL_ENV, "http://web:8082")


def _request_ids(scripted: _Scripted) -> list[str]:
    return [json.loads(request.content)["request_id"] for request in scripted.requests]


async def test_an_allowed_answer_allows_at_once() -> None:
    scripted = _Scripted([_verdict(ALLOWED)])
    clock, sleep = _clock_and_sleep()
    check = await ask_budget(job_id="job-1", clock=clock, sleep=sleep, transport=scripted)
    assert check.ok is True
    assert check.limit is None
    assert check.message is None
    assert len(scripted.requests) == 1
    body = json.loads(scripted.requests[0].content)
    assert body["job_id"] == "job-1"
    assert scripted.requests[0].headers[EXEC_TOKEN_HEADER] == "job-1.token"
    assert scripted.requests[0].url.path == "/budget"
    assert clock() == 0.0  # no retry


async def test_a_reached_limit_stops_with_the_limit_named() -> None:
    scripted = _Scripted([_verdict(JOB_CAP)])
    clock, sleep = _clock_and_sleep()
    check = await ask_budget(job_id="job-1", clock=clock, sleep=sleep, transport=scripted)
    assert check.ok is False
    assert check.limit == "job_cap"
    assert check.message == JOB_CAP["message"]
    assert len(scripted.requests) == 1


async def test_transient_failures_then_success_within_the_window() -> None:
    scripted = _Scripted([httpx.ConnectError, 500, _verdict(ALLOWED)])
    clock, sleep = _clock_and_sleep()
    check = await ask_budget(job_id="job-1", clock=clock, sleep=sleep, transport=scripted)
    assert check.ok is True
    assert len(scripted.requests) == 3
    ids = _request_ids(scripted)
    assert len(set(ids)) == 3  # each request carries its own fresh id
    assert clock() > 0  # the retries waited with backoff


async def test_the_window_exhausts_and_stops_saying_the_budget_could_not_be_checked() -> None:
    scripted = _Scripted([500])
    clock, sleep = _clock_and_sleep()
    check = await ask_budget(
        job_id="job-1", window=100.0, clock=clock, sleep=sleep, transport=scripted
    )
    assert check.ok is False
    assert check.limit is None
    assert check.message is not None
    assert "could not be checked" in check.message
    assert len(scripted.requests) > 1  # retried until the window
    assert 90.0 <= clock() <= 100.0


async def test_an_authentication_refusal_stops_at_once() -> None:
    scripted = _Scripted([401])
    clock, sleep = _clock_and_sleep()
    check = await ask_budget(job_id="job-1", clock=clock, sleep=sleep, transport=scripted)
    assert check.ok is False
    assert check.limit is None
    assert check.message is not None
    assert "could not be checked" in check.message
    assert len(scripted.requests) == 1  # no retry
    assert clock() == 0.0


async def test_a_reply_quoting_the_wrong_request_id_is_invalid() -> None:
    def wrong_id(request: httpx.Request) -> dict[str, object]:
        return {"request_id": "another-request", **JOB_CAP}

    scripted = _Scripted([wrong_id])
    clock, sleep = _clock_and_sleep()
    check = await ask_budget(
        job_id="job-1", window=50.0, clock=clock, sleep=sleep, transport=scripted
    )
    assert check.ok is False
    assert check.limit is None  # the invalid reply never counted as a verdict
    assert check.message is not None
    assert "could not be checked" in check.message
    ids = _request_ids(scripted)
    assert len(set(ids)) == len(ids)  # a fresh id per request
    assert "another-request" not in ids


async def test_a_malformed_reply_is_retried_not_stopped_on() -> None:
    scripted = _Scripted(["not json", _verdict(ALLOWED)])
    clock, sleep = _clock_and_sleep()
    check = await ask_budget(job_id="job-1", clock=clock, sleep=sleep, transport=scripted)
    assert check.ok is True
    assert len(scripted.requests) == 2


class _Hanging(httpx.AsyncBaseTransport):
    """A web that accepts the connection and then never answers."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200, request=request)


async def test_a_request_never_waits_past_the_remaining_window() -> None:
    """A hung web is cut off by the request's own timeout, so the total stays
    within the window."""
    started = time.monotonic()
    check = await ask_budget(job_id="job-1", window=2.0, transport=_Hanging())
    assert check.ok is False
    assert time.monotonic() - started < 30.0

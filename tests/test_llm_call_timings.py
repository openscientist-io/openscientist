"""The proxy records how long each Messages call took upstream."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp

from openscientist import llm_proxy
from openscientist.database.models import CostRecord, Job, LlmCallTiming
from openscientist.llm_proxy import create_llm_proxy_app
from openscientist.providers.base import LlmUpstream, StaticHeadersSigning
from tests import test_llm_proxy as proxy_fixtures
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.test_budget import committed, job_with_costs

placeholder_for = proxy_fixtures.placeholder_for

_START = {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 1}}}
_PING = {"type": "ping"}
_DELTA = {"type": "message_delta", "usage": {"output_tokens": 34}}
_STOP = {"type": "message_stop"}


@pytest.fixture(autouse=True)
async def no_limits(test_engine, monkeypatch):
    await set_admin_settings_row(test_engine)
    monkeypatch.setattr("openscientist.budget._provider_billing", _no_billing)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


async def _no_billing():
    return None


def _events(*events: dict) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


class _PacedStream(httpx.AsyncByteStream):
    """Upstream body parts, with a float a pause and an exception a broken connection."""

    def __init__(self, parts: list[bytes | float | Exception]) -> None:
        self.parts = parts

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            if isinstance(part, float):
                await asyncio.sleep(part)
            elif isinstance(part, Exception):
                raise part
            else:
                yield part


def _proxy(handler) -> ASGIApp:
    return create_llm_proxy_app(
        upstream=lambda: LlmUpstream(
            "https://upstream.test", StaticHeadersSigning({"x-api-key": "REAL"})
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        provider_id=lambda: "anthropic",
    )


def _streaming(parts: list[bytes | float | Exception], *, headers_after: float = 0.0):
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(headers_after)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_PacedStream(parts)
        )

    return handler


async def _call(app: ASGIApp, placeholder: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        return await client.post(
            "/v1/messages",
            headers={"x-api-key": placeholder},
            json={"model": "claude-sonnet-4-6", "messages": [], "max_tokens": 10, "stream": True},
        )


@asynccontextmanager
async def _job(engine: AsyncEngine) -> AsyncIterator[Job]:
    async with job_with_costs(engine, []) as job:
        try:
            yield job
        finally:
            async with committed(engine) as session:
                await session.execute(delete(LlmCallTiming).where(LlmCallTiming.job_id == job.id))
                await session.commit()


async def _timing(engine: AsyncEngine, job: Job) -> LlmCallTiming:
    async with committed(engine) as session:
        (timing,) = (
            await session.scalars(select(LlmCallTiming).where(LlmCallTiming.job_id == job.id))
        ).all()
        return timing


async def test_a_streamed_call_records_its_header_first_event_and_longest_gap_times(
    test_engine, placeholder_for
):
    async with _job(test_engine) as job:
        response = await _call(
            _proxy(
                _streaming(
                    [_events(_START), 0.5, _events(_PING, _DELTA), 0.1, _events(_STOP)],
                    headers_after=0.3,
                )
            ),
            placeholder_for(str(job.id)),
        )
        assert response.status_code == 200
        timing = await _timing(test_engine, job)
        async with committed(test_engine) as session:
            cost_id = await session.scalar(select(CostRecord.id).where(CostRecord.job_id == job.id))
    assert timing.cost_record_id == cost_id is not None
    assert (timing.run_kind, timing.model, timing.streamed, timing.status_code) == (
        "discovery",
        "claude-sonnet-4-6",
        True,
        200,
    )
    assert 0.3 <= timing.headers_seconds < 0.5
    assert timing.headers_seconds <= timing.first_event_seconds < 0.5
    assert 0.5 <= timing.longest_event_gap_seconds < 0.6
    assert timing.duration_seconds >= 0.9
    assert (timing.input_tokens, timing.output_tokens, timing.ended) == (12, 34, "complete")


@pytest.mark.parametrize(
    ("parts", "ended"),
    [
        ([_events(_START, _DELTA)], "upstream_ended_early"),
        ([_events(_START), httpx.ReadError("connection reset")], "upstream_ended_early"),
    ],
)
async def test_a_stream_that_ends_without_message_stop_ended_early(
    parts, ended, test_engine, placeholder_for
):
    async with _job(test_engine) as job:
        try:
            await _call(_proxy(_streaming(parts)), placeholder_for(str(job.id)))
        except httpx.ReadError:
            pass
        timing = await _timing(test_engine, job)
    assert timing.ended == ended
    assert (timing.input_tokens, timing.output_tokens) == (None, None)
    assert timing.cost_record_id is not None


@pytest.mark.parametrize("stage", ["headers", "body", "end"])
@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
async def test_a_client_that_leaves_is_recorded_as_disconnected(
    stage, error, test_engine, placeholder_for
):
    app = _proxy(_streaming([_events(_START), 0.05, _events(_DELTA, _STOP)]))

    async def leaving(scope, receive, send):
        async def send_until_stage(message):
            current = (
                "headers"
                if message["type"] == "http.response.start"
                else "body"
                if message.get("more_body")
                else "end"
            )
            if current == stage:
                raise error("client disconnected")
            await send(message)

        await app(scope, receive, send_until_stage)

    async with _job(test_engine) as job:
        with pytest.raises(error):
            await _call(leaving, placeholder_for(str(job.id)))
        timing = await _timing(test_engine, job)
    assert timing.ended == "client_disconnected"
    assert (timing.first_event_seconds is None) is (stage == "headers")
    assert (timing.input_tokens, timing.output_tokens) == (None, None)


async def test_an_unbilled_error_is_recorded_without_a_cost_record(test_engine, placeholder_for):
    async def overloaded(request: httpx.Request) -> httpx.Response:
        error = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        return httpx.Response(
            529,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(error).encode()),
        )

    async with _job(test_engine) as job:
        response = await _call(_proxy(overloaded), placeholder_for(str(job.id)))
        assert response.status_code == 529
        timing = await _timing(test_engine, job)
    assert (timing.status_code, timing.streamed, timing.ended) == (529, False, "complete")
    assert timing.cost_record_id is None
    assert timing.first_event_seconds is None
    assert timing.longest_event_gap_seconds is None


@pytest.mark.parametrize(
    ("error", "ended", "charged"),
    [
        (httpx.ConnectError("unreachable"), "upstream_ended_early", False),
        (httpx.ReadTimeout("no headers"), "upstream_ended_early", True),
        (asyncio.CancelledError(), "client_disconnected", True),
    ],
)
async def test_a_call_without_response_headers_is_recorded_without_a_status(
    error, ended, charged, test_engine, placeholder_for
):
    async def failing(request: httpx.Request) -> httpx.Response:
        raise error

    async with _job(test_engine) as job:
        if isinstance(error, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await _call(_proxy(failing), placeholder_for(str(job.id)))
        else:
            assert (await _call(_proxy(failing), placeholder_for(str(job.id)))).status_code == 502
        timing = await _timing(test_engine, job)
    assert (timing.status_code, timing.headers_seconds, timing.ended) == (None, None, ended)
    assert (timing.cost_record_id is not None) is charged


@pytest.mark.parametrize("threshold", [0.2, 5.0])
async def test_a_long_pause_between_events_logs_a_warning(
    threshold, test_engine, placeholder_for, monkeypatch, caplog
):
    monkeypatch.setattr(llm_proxy, "_STALLED_EVENT_GAP_SECONDS", threshold)
    caplog.set_level(logging.WARNING, logger="openscientist.llm_proxy")
    async with _job(test_engine) as job:
        await _call(
            _proxy(_streaming([_events(_START), 0.4, _events(_DELTA, _STOP)])),
            placeholder_for(str(job.id)),
        )
        await _timing(test_engine, job)
    warnings = [
        record.message for record in caplog.records if "between stream events" in record.message
    ]
    assert len(warnings) == (1 if threshold < 0.4 else 0)
    if warnings:
        assert str(job.id) in warnings[0]


async def test_the_timing_outlives_its_job(test_engine, placeholder_for):
    async with _job(test_engine) as job:
        await _call(
            _proxy(_streaming([_events(_START, _DELTA, _STOP)])), placeholder_for(str(job.id))
        )
        timing_id = (await _timing(test_engine, job)).id
        async with committed(test_engine) as session:
            await session.execute(delete(Job).where(Job.id == job.id))
            await session.commit()
    async with committed(test_engine) as session:
        timing = await session.get(LlmCallTiming, timing_id)
        assert timing is not None
        assert timing.job_id is None
        assert timing.cost_record_id is not None
        await session.delete(timing)
        await session.execute(delete(CostRecord).where(CostRecord.id == timing.cost_record_id))
        await session.commit()

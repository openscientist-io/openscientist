"""The web's metering of every model call the LLM proxy forwards.

The proxy tees each response: the bytes and frames pass through unchanged
while a wire-format decoder accumulates the token buckets the response
carries, and at response end the web writes the cost row through its admin
session. The container writes no cost rows, so the tokens and the cost come
from the wire the proxy observed, not from the harness's word.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from openscientist.agent.base import TokenUsage
from openscientist.database.models import CostRecord
from openscientist.database.models.job import Job as JobModel
from openscientist.database.retry import retry_async
from openscientist.database.session import get_admin_session
from openscientist.providers.pricing import estimate_cost_usd

if TYPE_CHECKING:
    # Only a type here, since a runtime import loops back through job_container into the proxy.
    from openscientist.job_container.secrets import RunKind

logger = logging.getLogger(__name__)

#: How long a failed cost write keeps retrying a lost database connection.
_WRITE_RETRY_SECONDS = 10.0

#: The job status that marks a run in its report phase.
_GENERATING_REPORT = "generating_report"


@dataclass(frozen=True)
class _ObservedCall:
    """One model call as the wire reported it, ready for its cost row."""

    model: str
    buckets: TokenUsage


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _anthropic_buckets(usage: dict) -> TokenUsage:
    """The Anthropic-shaped usage (Messages, Bedrock, Vertex) as the billing
    buckets. The one-hour share of the cache writes is clamped to the total,
    so the two write tiers stay disjoint and sum to it."""
    cache_write_total = _int(usage.get("cache_creation_input_tokens"))
    write_1h = min(
        _int(_dict(usage.get("cache_creation")).get("ephemeral_1h_input_tokens")),
        cache_write_total,
    )
    return TokenUsage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_write_tokens=cache_write_total - write_1h,
        cache_write_1h_tokens=write_1h,
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
    )


def _openai_chat_buckets(usage: dict) -> TokenUsage:
    """The OpenAI chat-completions usage as the billing buckets. The prompt
    count includes the cached part and the completion count the reasoning
    part, so both are subtracted to keep the buckets non-overlapping."""
    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    cached = _int(_dict(usage.get("prompt_tokens_details")).get("cached_tokens"))
    reasoning = _int(_dict(usage.get("completion_tokens_details")).get("reasoning_tokens"))
    return TokenUsage(
        input_tokens=prompt - cached,
        output_tokens=max(completion - reasoning, 0),
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
    )


def _responses_buckets(usage: dict) -> TokenUsage:
    """The OpenAI Responses usage as the billing buckets, the same subtraction
    as the chat wire: input carries the cached part, output the reasoning part."""
    input_tokens = _int(usage.get("input_tokens"))
    cached = _int(_dict(usage.get("input_tokens_details")).get("cached_tokens"))
    output = _int(usage.get("output_tokens"))
    reasoning = _int(_dict(usage.get("output_tokens_details")).get("reasoning_tokens"))
    return TokenUsage(
        input_tokens=input_tokens - cached,
        output_tokens=max(output - reasoning, 0),
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
    )


class _SseStream:
    """Split a byte stream into server-sent event payloads, tolerant of
    arbitrary chunking between lines and between events."""

    def __init__(self) -> None:
        self._pending = b""
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        self._pending += chunk
        events: list[str] = []
        while b"\n" in self._pending:
            raw_line, self._pending = self._pending.split(b"\n", 1)
            line = raw_line.rstrip(b"\r").decode("utf-8", "replace")
            if line.startswith("data:"):
                self._data.append(line[5:].removeprefix(" "))
            elif not line:
                if self._data:
                    events.append("\n".join(self._data))
                    self._data = []
        return events

    def flush(self) -> list[str]:
        """The event a stream ended with, if it ended on a data line without
        the terminating blank line."""
        if not self._data:
            return []
        events = ["\n".join(self._data)]
        self._data = []
        return events


class _JsonDecoder:
    """A non-streaming JSON body, metered at its end. The body must carry a
    ``usage`` field for the row to hold anything but the zero buckets."""

    def __init__(self, model: str, buckets_from_usage) -> None:
        self._model = model
        self._buckets_from_usage = buckets_from_usage
        self._body = b""

    def feed(self, chunk: bytes) -> None:
        self._body += chunk

    def finalize(self, outcome: str) -> tuple[list[_ObservedCall], str | None]:
        try:
            parsed = json.loads(self._body)
        except ValueError:
            return [
                _ObservedCall(self._model, TokenUsage())
            ], "response body was not parseable as JSON"
        usage = _dict(parsed).get("usage")
        if not isinstance(usage, dict):
            return [_ObservedCall(self._model, TokenUsage())], None
        return [_ObservedCall(self._model, self._buckets_from_usage(usage))], None


class _AnthropicSseDecoder:
    """The Anthropic Messages stream (also Vertex's streamRawPredict).

    ``message_start`` carries the input and cache buckets the provider bills
    even when the stream is cut, and ``message_delta`` the final output count.
    A stream that ends without ``message_delta`` still writes its row with
    the buckets observed so far."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._sse = _SseStream()
        self._buckets = TokenUsage()
        self._has_final = False
        self._error = False

    def feed(self, chunk: bytes) -> None:
        for event in self._sse.feed(chunk):
            self._handle(event)

    def _handle(self, data: str) -> None:
        try:
            obj = json.loads(data)
        except ValueError:
            return
        kind = _dict(obj).get("type")
        if kind == "message_start":
            usage = _dict(_dict(obj).get("message")).get("usage")
            if isinstance(usage, dict):
                self._buckets = _anthropic_buckets(usage)
        elif kind == "message_delta":
            self._buckets.output_tokens = _int(_dict(_dict(obj).get("usage")).get("output_tokens"))
            self._has_final = True
        elif kind == "error":
            self._error = True

    def finalize(self, outcome: str) -> tuple[list[_ObservedCall], str | None]:
        for event in self._sse.flush():
            self._handle(event)
        if self._error:
            reason = "stream carried an upstream error"
        elif not self._has_final:
            reason = "stream ended without its final usage"
        else:
            reason = None
        return [_ObservedCall(self._model, self._buckets)], reason


class _BedrockStreamDecoder:
    """The Bedrock response stream, the binary ``application/vnd.amazon.eventstream``
    framing the gateway must pass through unconverted. The frames are parsed
    only to read ``message_start`` and ``message_delta`` out of the payloads;
    the wire itself is untouched. A cut stream keeps its start buckets, and an
    ``exception`` frame is logged with the partial row."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._buffer = b""
        self._buckets = TokenUsage()
        self._has_final = False
        self._error = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while len(self._buffer) >= 20:
            total = int.from_bytes(self._buffer[:4], "big")
            headers_len = int.from_bytes(self._buffer[4:8], "big")
            payload_len = int.from_bytes(self._buffer[12:16], "big")
            # The 20-byte prelude plus the trailing 4-byte message checksum are
            # the smallest frame; the payload lengths come from the prelude.
            if (
                total < 24
                or headers_len > total - 24
                or payload_len > total - 24 - headers_len
                or len(self._buffer) < total
            ):
                break
            self._handle(self._buffer[20 + headers_len : 20 + headers_len + payload_len])
            self._buffer = self._buffer[total:]

    def _handle(self, payload: bytes) -> None:
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        kind = _dict(obj).get("type")
        if kind == "message_start":
            usage = _dict(_dict(obj).get("message")).get("usage")
            if isinstance(usage, dict):
                self._buckets = _anthropic_buckets(usage)
        elif kind == "message_delta":
            self._buckets.output_tokens = _int(_dict(_dict(obj).get("usage")).get("output_tokens"))
            self._has_final = True
        elif kind == "exception":
            self._error = True

    def finalize(self, outcome: str) -> tuple[list[_ObservedCall], str | None]:
        if self._error:
            reason = "stream carried an upstream exception"
        elif not self._has_final:
            reason = "stream ended without its final usage"
        else:
            reason = None
        return [_ObservedCall(self._model, self._buckets)], reason


class _ChatSseDecoder:
    """The OpenAI chat-completions stream. The chunks carry no usage except
    the final one, and only when the request asked for it. The stream is only
    complete once it sends ``[DONE]``, so a completed stream with no usage
    writes the zero row, and a stream that ends or is cut before it writes
    none."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._sse = _SseStream()
        self._buckets: TokenUsage | None = None
        self._error = False
        self._done = False

    def feed(self, chunk: bytes) -> None:
        for event in self._sse.feed(chunk):
            self._handle(event)

    def _handle(self, data: str) -> None:
        if data == "[DONE]":
            self._done = True
            return
        try:
            obj = json.loads(data)
        except ValueError:
            return
        obj = _dict(obj)
        if obj.get("error") is not None:
            self._error = True
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._buckets = _openai_chat_buckets(usage)

    def finalize(self, outcome: str) -> tuple[list[_ObservedCall], str | None]:
        for event in self._sse.flush():
            self._handle(event)
        if self._buckets is not None:
            return [_ObservedCall(self._model, self._buckets)], None
        if not self._done:
            reason = (
                "stream ended without its [DONE]"
                if outcome == "end"
                else "stream cut with no usage"
            )
            return [], reason
        reason = "stream ended with no usage"
        if self._error:
            reason += " and an upstream error"
        return [_ObservedCall(self._model, TokenUsage())], reason


class _ResponsesDecoder:
    """The OpenAI Responses stream, the REST SSE and the websocket alike.
    Usage is reported only at completion, so each ``response.completed``
    event meters one call (a websocket turn can carry several), and a stream
    that ends without one writes no row. The REST body streams through
    ``feed``. The websocket path feeds one upstream frame per ``feed_frame``
    call, and a frame that is a single JSON object is a complete event, since
    the codex wire carries no SSE framing."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._sse = _SseStream()
        self._calls: list[_ObservedCall] = []
        self._completed = False
        self._failed = False

    def feed(self, chunk: bytes) -> None:
        for event in self._sse.feed(chunk):
            self._handle(event)

    def feed_frame(self, data: bytes) -> None:
        """One websocket frame. A frame whose text is a single JSON object is
        a complete Responses event and is decoded directly. Everything else,
        SSE ``data:`` lines included, goes through the stream splitter."""
        text = data.lstrip()
        if text.startswith(b"{"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                self._handle(data.decode("utf-8", "replace"))
                return
        self.feed(data)

    def _handle(self, data: str) -> None:
        try:
            obj = json.loads(data)
        except ValueError:
            return
        kind = _dict(obj).get("type")
        if kind == "response.failed":
            self._failed = True
        elif kind == "response.completed":
            response = _dict(obj).get("response")
            response = _dict(response)
            model = response.get("model")
            if not isinstance(model, str) or not model:
                model = self._model
            usage = response.get("usage")
            buckets = _responses_buckets(usage) if isinstance(usage, dict) else TokenUsage()
            self._calls.append(_ObservedCall(model, buckets))
            self._completed = True

    def take_completed(self) -> list[_ObservedCall]:
        """The completed calls not yet metered, for the websocket path that
        writes a row per completed response as it arrives."""
        calls, self._calls = self._calls, []
        return calls

    def saw_completion(self) -> bool:
        return self._completed

    def finalize(self, outcome: str) -> tuple[list[_ObservedCall], str | None]:
        for event in self._sse.flush():
            self._handle(event)
        calls, self._calls = self._calls, []
        if calls:
            return calls, None
        reason = (
            "the response failed before completion"
            if self._failed
            else "stream ended without a completed response"
        )
        return [], reason


def _usage_decoder(
    path: str, content_type: str, model: str
) -> (
    _JsonDecoder
    | _AnthropicSseDecoder
    | _BedrockStreamDecoder
    | _ChatSseDecoder
    | _ResponsesDecoder
    | None
):
    """The decoder for a forwarded request, chosen from the request path and
    the upstream content type, or None when the path is not a model call the
    proxy meters (a count-tokens lookup)."""
    if "count-tokens" in path:
        return None
    if path.endswith(":streamRawPredict"):
        return _AnthropicSseDecoder(model)
    if path.endswith(":rawPredict"):
        return _JsonDecoder(model, _anthropic_buckets)
    if path.endswith("/invoke-with-response-stream"):
        return _BedrockStreamDecoder(model)
    if path.endswith("/invoke"):
        return _JsonDecoder(model, _anthropic_buckets)
    if path.rsplit("/", 1)[-1] == "responses":
        return _ResponsesDecoder(model)
    streaming = "text/event-stream" in content_type
    if path.endswith("/chat/completions"):
        if streaming:
            return _ChatSseDecoder(model)
        return _JsonDecoder(model, _openai_chat_buckets)
    if streaming:
        return _AnthropicSseDecoder(model)
    return _JsonDecoder(model, _anthropic_buckets)


def _model_from_body(body: bytes) -> str | None:
    """The ``model`` field of a JSON request body, per wire format."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    model = _dict(parsed).get("model")
    if isinstance(model, str) and model:
        return model
    return None


def _model_from_path(path: str) -> str:
    """The model the request path names, for the wires whose path carries it
    (Bedrock invoke, Vertex predict)."""
    if path.startswith("/model/"):
        model = path.removeprefix("/model/").rsplit("/", 1)[0]
        if model:
            return model
    last = path.rsplit("/", 1)[-1]
    for suffix in (":streamRawPredict", ":rawPredict"):
        if last.endswith(suffix):
            model = last.removesuffix(suffix)
            if model:
                return model
    return "unknown"


def _operation_labels(job: JobModel | None, kind: RunKind) -> tuple[str, int | None]:
    """The row's operation type and iteration from the job's state at write
    time. A chat run is a chat row, and a discovery or report run takes the
    report label when the job is in its report phase, with the iteration the
    job holds. The labels are display and breakdown only. A wrong one moves a
    row within the billing breakdown, and it cannot move a total, because the
    tokens and the cost come from the wire, and no budget or scheduler path
    reads them."""
    if kind == "chat":
        return "chat", None
    operation = "report" if job is not None and job.status == _GENERATING_REPORT else "discovery"
    return operation, job.current_iteration if job is not None else None


async def record_model_call(
    *,
    job_id: str,
    kind: RunKind,
    model: str,
    provider_id: str,
    buckets: TokenUsage,
) -> None:
    """Write the cost row of one observed model call, as the web's admin
    login. A lost database connection is retried, and a write that still
    fails logs every bucket so the call can still be billed by hand. It never
    raises, so a metering failure cannot drop a forwarded response."""
    try:
        uid = UUID(job_id)
    except ValueError:
        logger.error(
            "Cost of job %s was not saved: the job id is not a UUID "
            "(provider=%s model=%s input=%d output=%d cache_read=%d cache_write=%d reasoning=%d)",
            job_id,
            provider_id,
            model,
            buckets.input_tokens,
            buckets.output_tokens,
            buckets.cache_read_tokens,
            buckets.cache_write_tokens + buckets.cache_write_1h_tokens,
            buckets.reasoning_tokens,
        )
        return

    async def _write() -> None:
        async with get_admin_session() as session:
            job = await session.get(JobModel, uid)
            if job is None:
                logger.warning(
                    "Cost of job %s was not saved: the job no longer exists "
                    "(provider=%s model=%s input=%d output=%d)",
                    job_id,
                    provider_id,
                    model,
                    buckets.input_tokens,
                    buckets.output_tokens,
                )
                return
            operation, iteration = _operation_labels(job, kind)
            cost_usd = estimate_cost_usd(
                model,
                buckets.input_tokens,
                buckets.output_tokens,
                cache_read_tokens=buckets.cache_read_tokens,
                cache_write_tokens=buckets.cache_write_tokens,
                cache_write_1h_tokens=buckets.cache_write_1h_tokens,
                reasoning_tokens=buckets.reasoning_tokens,
            )
            session.add(
                CostRecord(
                    job_id=uid,
                    user_id=job.owner_id,
                    iteration=iteration,
                    operation_type=operation,
                    provider=provider_id,
                    model=model,
                    input_tokens=buckets.input_tokens,
                    output_tokens=buckets.output_tokens,
                    cache_read_tokens=buckets.cache_read_tokens,
                    # The column holds every cache write. Only the price keeps
                    # the tiers apart, and `cost_usd` already does.
                    cache_write_tokens=buckets.cache_write_tokens + buckets.cache_write_1h_tokens,
                    reasoning_tokens=buckets.reasoning_tokens,
                    cost_usd=cost_usd,
                )
            )
            await session.commit()

    try:
        await retry_async(
            _write, within=_WRITE_RETRY_SECONDS, what=f"writing the cost of job {job_id}"
        )
    except Exception as e:
        logger.error(
            "Cost of job %s was not saved: provider=%s model=%s input=%d output=%d "
            "cache_write=%d cache_write_1h=%d cache_read=%d reasoning=%d (%s)",
            job_id,
            provider_id,
            model,
            buckets.input_tokens,
            buckets.output_tokens,
            buckets.cache_write_tokens,
            buckets.cache_write_1h_tokens,
            buckets.cache_read_tokens,
            buckets.reasoning_tokens,
            e,
        )

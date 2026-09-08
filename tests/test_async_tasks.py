"""The sync/async bridge's timeout must actually bound the call."""

import asyncio
import concurrent.futures
import time
from collections.abc import AsyncIterator

import pytest

from openscientist.async_tasks import run_sync

_GRACE_SECONDS = 3.0
_BODY_SECONDS = 30.0


class _Body:
    """A slow coroutine that records whether it was cancelled."""

    def __init__(self) -> None:
        self.cancelled = False
        self.finished = False

    async def run(self) -> str:
        try:
            await asyncio.sleep(_BODY_SECONDS)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        return "done"


@pytest.mark.asyncio
async def test_timeout_returns_at_the_deadline_from_a_running_loop() -> None:
    """The caller waited for the whole coroutine before reporting its timeout,
    so a stalled write failed the caller long after it had already finished."""
    body = _Body()
    started = time.monotonic()
    with pytest.raises(concurrent.futures.TimeoutError):
        run_sync(body.run(), timeout=0.5)
    assert time.monotonic() - started < _GRACE_SECONDS


@pytest.mark.asyncio
async def test_timeout_cancels_the_work_from_a_running_loop() -> None:
    """Reporting a timeout while the coroutine keeps mutating the database is
    what let a failed run still commit its write."""
    body = _Body()
    with pytest.raises(concurrent.futures.TimeoutError):
        run_sync(body.run(), timeout=0.5)
    assert body.cancelled
    assert not body.finished


def test_timeout_applies_without_a_running_loop() -> None:
    """The plain path took the same timeout argument and ignored it."""
    body = _Body()
    started = time.monotonic()
    with pytest.raises(concurrent.futures.TimeoutError):
        run_sync(body.run(), timeout=0.5)
    assert time.monotonic() - started < _GRACE_SECONDS
    assert body.cancelled


async def _answer() -> str:
    await asyncio.sleep(0)
    return "answer"


@pytest.mark.asyncio
async def test_a_result_still_comes_back_from_a_running_loop() -> None:
    assert run_sync(_answer()) == "answer"


def test_a_result_still_comes_back_without_a_running_loop() -> None:
    assert run_sync(_answer()) == "answer"


async def _numbers(closed: dict[str, bool]) -> AsyncIterator[int]:
    try:
        yield 1
        yield 2
    finally:
        closed["closed"] = True


async def _take_one(closed: dict[str, bool], kept: list[AsyncIterator[int]]) -> str:
    """Keep the generator alive past the coroutine, so nothing collects it and
    only the loop's own shutdown can finalise it."""
    numbers = _numbers(closed)
    kept.append(numbers)
    async for _ in numbers:
        break
    return "took one"


@pytest.mark.asyncio
async def test_a_live_async_generator_is_finalised_at_shutdown() -> None:
    """``asyncio.run`` shuts down async generators before the loop goes away.
    A hand-rolled loop must do the same, or a suspended generator never runs
    its ``finally`` and whatever it holds is never released."""
    closed: dict[str, bool] = {}
    kept: list[AsyncIterator[int]] = []
    assert run_sync(_take_one(closed, kept)) == "took one"
    assert closed.get("closed")

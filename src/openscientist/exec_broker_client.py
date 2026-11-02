"""HTTP client for the web-side execution broker (httpx only, no docker).
The execute_code tool posts here instead of spawning containers over a socket.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from openscientist.settings import get_settings

_WEB_HOST_ENV = "OPENSCIENTIST_WEB_HOST"
_DEFAULT_WEB_HOST = "openscientist"

EXEC_TOKEN_ENV = "OPENSCIENTIST_EXEC_TOKEN"
EXEC_BROKER_URL_ENV = "OPENSCIENTIST_EXEC_BROKER_URL"
EXEC_TOKEN_HEADER = "x-openscientist-exec-token"

# Wait past the executor's own timeout so a slow run still returns its result.
_HTTP_TIMEOUT_MARGIN = 60.0

#: A budget check retries this long before it stops the job as a reached limit.
BUDGET_CHECK_WINDOW_SECONDS = 120.0
#: A single budget request never waits longer than this, so the window bounds the total.
_BUDGET_REQUEST_TIMEOUT_SECONDS = 30.0
#: Retries start this far apart and double, so a dead web is not hammered.
_BUDGET_RETRY_BASE_SECONDS = 1.0
_BUDGET_RETRY_CAP_SECONDS = 15.0


class BrokerError(RuntimeError):
    """Raised when the broker call fails at the transport or protocol layer."""


@dataclass(frozen=True)
class BudgetCheck:
    """The web's answer to a budget check.

    When the web never gave a usable answer, ``ok`` is False and ``message``
    says the budget could not be checked and why.
    """

    ok: bool
    limit: str | None
    message: str | None


def container_broker_base_url() -> str:
    """Broker base URL as reached from a sibling job container on the compose network."""
    host = os.environ.get(_WEB_HOST_ENV, _DEFAULT_WEB_HOST)
    return f"http://{host}:{get_settings().container.exec_broker_port}"


def execute_code_via_broker(
    *,
    code: str,
    language: str,
    job_id: str,
    output_dir: str,
    timeout: int,
    data_path: str | None = None,
    data_files: list[dict[str, Any]] | None = None,
    description: str = "",
    iteration: int = 0,
) -> dict[str, Any]:
    """POST an execute_code request (host-absolute paths) and return its result dict."""
    base_url = os.environ.get(EXEC_BROKER_URL_ENV) or container_broker_base_url()
    token = os.environ.get(EXEC_TOKEN_ENV, "")
    payload: dict[str, Any] = {
        "code": code,
        "language": language,
        "job_id": job_id,
        "output_dir": output_dir,
        "data_path": data_path,
        "data_files": data_files or [],
        "description": description,
        "iteration": iteration,
        "timeout": timeout,
    }
    try:
        response = httpx.post(
            f"{base_url}/execute",
            json=payload,
            headers={EXEC_TOKEN_HEADER: token},
            timeout=float(timeout) + _HTTP_TIMEOUT_MARGIN,
        )
    except httpx.HTTPError as exc:
        raise BrokerError(f"execution broker request failed: {exc}") from exc
    if response.status_code != 200:
        raise BrokerError(
            f"execution broker returned HTTP {response.status_code}: {response.text[:500]}"
        )
    try:
        result: dict[str, Any] = response.json()
    except ValueError as exc:
        raise BrokerError(f"execution broker returned invalid JSON: {exc}") from exc
    return result


def _unchecked(reason: str) -> BudgetCheck:
    """A stop with the note saying the budget could not be checked and why."""
    return BudgetCheck(False, None, f"the budget could not be checked: {reason}")


def _parse_budget_check(response: httpx.Response, request_id: str) -> BudgetCheck | None:
    """The reply's verdict, or None when it is not this request's verdict.

    A reply that quotes no request, or the wrong one, is invalid, and so is a
    reply whose fields are not the verdict's shape.
    """
    try:
        raw: Any = response.json()
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("request_id") != request_id:
        return None
    ok = raw.get("ok")
    limit = raw.get("limit")
    message = raw.get("message")
    if not isinstance(ok, bool):
        return None
    if ok:
        if limit is not None or message is not None:
            return None
        return BudgetCheck(True, None, None)
    if not isinstance(limit, str) or not isinstance(message, str) or not message:
        return None
    return BudgetCheck(False, limit, message)


async def ask_budget(
    *,
    job_id: str,
    window: float = BUDGET_CHECK_WINDOW_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BudgetCheck:
    """Ask the web whether the job may run its next unit of work.

    Each request carries a fresh id the reply must quote. A no-answer, an
    error status or an invalid reply is retried with backoff until ``window``
    seconds have passed, after which the job stops as a reached limit with the
    note saying the budget could not be checked. An authentication refusal
    stops at once, with the same note.
    """
    base_url = os.environ.get(EXEC_BROKER_URL_ENV) or container_broker_base_url()
    token = os.environ.get(EXEC_TOKEN_ENV, "")
    deadline = clock() + window
    retry = _BUDGET_RETRY_BASE_SECONDS
    last_failure = "the web did not answer the budget check"
    async with httpx.AsyncClient(transport=transport) as client:
        while deadline - clock() > 0:
            request_id = uuid4().hex
            remaining = deadline - clock()
            try:
                # A hung connection would outlive the window, so the remaining
                # time bounds the wait whether or not the transport enforces it.
                response = await asyncio.wait_for(
                    client.post(
                        f"{base_url}/budget",
                        json={"job_id": job_id, "request_id": request_id},
                        headers={EXEC_TOKEN_HEADER: token},
                        timeout=min(_BUDGET_REQUEST_TIMEOUT_SECONDS, remaining),
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                last_failure = "the web did not answer the budget check"
            except httpx.HTTPError as exc:
                last_failure = f"the web could not be reached ({exc.__class__.__name__})"
            else:
                if response.status_code in (401, 403):
                    return _unchecked("the web refused the run's credentials")
                elif response.status_code != 200:
                    last_failure = (
                        f"the web answered the budget check with HTTP {response.status_code}"
                    )
                else:
                    check = _parse_budget_check(response, request_id)
                    if check is not None:
                        return check
                    last_failure = "the web's reply did not quote the request"
            await sleep(max(0.0, min(retry, deadline - clock())))
            retry = min(retry * 2, _BUDGET_RETRY_CAP_SECONDS)
    return _unchecked(last_failure)

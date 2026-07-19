"""HTTP client for the web-side execution broker (httpx only, no docker).
The execute_code tool posts here instead of spawning containers over a socket.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

EXEC_BROKER_PORT = 8082
_WEB_HOST_ENV = "OPENSCIENTIST_WEB_HOST"
_DEFAULT_WEB_HOST = "openscientist"

EXEC_TOKEN_ENV = "OPENSCIENTIST_EXEC_TOKEN"
EXEC_BROKER_URL_ENV = "OPENSCIENTIST_EXEC_BROKER_URL"
EXEC_TOKEN_HEADER = "x-openscientist-exec-token"

# Wait past the executor's own timeout so a slow run still returns its result.
_HTTP_TIMEOUT_MARGIN = 60.0


class BrokerError(RuntimeError):
    """Raised when the broker call fails at the transport or protocol layer."""


def container_broker_base_url() -> str:
    """Broker base URL as reached from a sibling job container on the compose network."""
    host = os.environ.get(_WEB_HOST_ENV, _DEFAULT_WEB_HOST)
    return f"http://{host}:{EXEC_BROKER_PORT}"


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

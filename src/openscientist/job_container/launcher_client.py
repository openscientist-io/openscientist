"""The web's client for the container launcher, over its guarded unix socket.

One synchronous method per launcher endpoint, each raising a typed error the
call sites catch the way they catch Docker errors today.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict, cast

import httpx

from openscientist.settings import get_settings

#: The launcher socket the compose stack shares between the web and the launcher.
DEFAULT_SOCKET_PATH = "/run/launcher/launcher.sock"

_CONTROL_TIMEOUT_SECONDS = 30.0
_CREATE_TIMEOUT_SECONDS = 60.0
_STOP_TIMEOUT_SECONDS = 90.0
_EXEC_EXTRA_TIMEOUT_SECONDS = 60.0


class LauncherError(Exception):
    """A launcher answer that is not a success, carrying its stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LauncherUnavailableError(LauncherError):
    """The launcher's socket could not be reached at all."""


class LauncherNotFoundError(LauncherError):
    """The launcher answers 404: the container is foreign to it or missing."""


class LauncherBadRequestError(LauncherError):
    """The launcher answers 400: a request field it refuses."""


class LauncherImageMissingError(LauncherError):
    """The launcher answers 409: a template image is not on the host."""


class LauncherDockerError(LauncherError):
    """The launcher answers 502: the daemon answered but the operation failed."""


_STATUS_TO_ERROR: dict[int, type[LauncherError]] = {
    400: LauncherBadRequestError,
    404: LauncherNotFoundError,
    409: LauncherImageMissingError,
    502: LauncherDockerError,
}


def _failure(exc: httpx.HTTPError, timeout: float) -> str:
    """What went wrong with a request that got no answer, for the error message."""
    if isinstance(exc, httpx.TimeoutException):
        return f"did not answer within {timeout:g}s"
    return "could not be reached"


class HealthResponse(TypedDict):
    docker: bool
    images: dict[str, bool]


class ContainerEntry(TypedDict):
    id: str
    name: str
    status: str
    created: str
    labels: dict[str, str]


class ExecResponse(TypedDict):
    stdout: str
    exit_code: int | None


class CreateAgentResponse(TypedDict):
    id: str
    short_id: str
    name: str
    status: str


class InspectResponse(TypedDict):
    status: str
    exit_code: int | None


class WaitResponse(TypedDict):
    exit_code: int | None
    timeout: bool


class LogsResponse(TypedDict):
    logs: str


class LauncherClient:
    """One httpx client bound to the launcher's unix socket."""

    def __init__(self, socket_path: str | Path | None = None) -> None:
        if socket_path is None:
            socket_path = get_settings().container.launcher_socket_path
        self._socket_path = str(socket_path)
        self._http = httpx.Client(
            transport=httpx.HTTPTransport(uds=self._socket_path),
            base_url="http://launcher",
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        body: Mapping[str, object] | None = None,
        params: httpx.QueryParams | None = None,
    ) -> object:
        try:
            response = self._http.request(method, path, json=body, params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LauncherUnavailableError(
                "unavailable", f"the launcher at {self._socket_path} {_failure(exc, timeout)}"
            ) from exc
        if response.status_code >= 400:
            try:
                detail = str(response.json()["error"])
            except (ValueError, KeyError, TypeError):
                detail = response.text
            error_cls = _STATUS_TO_ERROR.get(response.status_code, LauncherError)
            raise error_cls(detail.partition(":")[0], detail)
        # The JSON body is dynamic by nature; each caller names its shape.
        return response.json()

    def health(self) -> HealthResponse:
        """The daemon's state and the presence of both template images."""
        return cast(
            "HealthResponse",
            self._request("GET", "/v1/health", timeout=_CONTROL_TIMEOUT_SECONDS),
        )

    def create_agent(self, body: Mapping[str, object]) -> CreateAgentResponse:
        """Create and start one agent or chat container from the agent template."""
        return cast(
            "CreateAgentResponse",
            self._request(
                "POST", "/v1/containers/agent", body=body, timeout=_CREATE_TIMEOUT_SECONDS
            ),
        )

    def run_exec(self, body: Mapping[str, object]) -> ExecResponse:
        """Run one executor container synchronously; the launcher removes it."""
        # The launcher holds the request for up to timeout_seconds and then
        # kills the container, so the read timeout must clear that bound.
        bound = body.get("timeout_seconds")
        timeout = _EXEC_EXTRA_TIMEOUT_SECONDS + (float(bound) if isinstance(bound, int) else 0.0)
        return cast("ExecResponse", self._request("POST", "/v1/exec", body=body, timeout=timeout))

    def list_containers(self, labels: list[str]) -> list[ContainerEntry]:
        """Launcher-created containers carrying every label, AND-combined."""
        return cast(
            "list[ContainerEntry]",
            self._request(
                "GET",
                "/v1/containers",
                timeout=_CONTROL_TIMEOUT_SECONDS,
                params=httpx.QueryParams([("label", label) for label in labels]),
            ),
        )

    def inspect(self, ref: str) -> InspectResponse:
        """Status and exit code of one container."""
        return cast(
            "InspectResponse",
            self._request("GET", f"/v1/containers/{ref}", timeout=_CONTROL_TIMEOUT_SECONDS),
        )

    def stop(self, ref: str, timeout_seconds: int = 10) -> None:
        """Stop one container, with a grace period capped by the launcher."""
        self._request(
            "POST",
            f"/v1/containers/{ref}/stop",
            body={"timeout_seconds": timeout_seconds},
            timeout=_STOP_TIMEOUT_SECONDS,
        )

    def remove(self, ref: str) -> None:
        """Force-remove one container."""
        self._request("POST", f"/v1/containers/{ref}/remove", timeout=_CONTROL_TIMEOUT_SECONDS)

    def wait(self, ref: str, timeout_seconds: int) -> WaitResponse:
        """Block until one container exits or the bound runs out."""
        return cast(
            "WaitResponse",
            self._request(
                "POST",
                f"/v1/containers/{ref}/wait",
                body={"timeout_seconds": timeout_seconds},
                timeout=_EXEC_EXTRA_TIMEOUT_SECONDS + timeout_seconds,
            ),
        )

    def logs(self, ref: str) -> str:
        """One-shot combined stdout and stderr of one container."""
        raw = cast(
            "LogsResponse",
            self._request("GET", f"/v1/containers/{ref}/logs", timeout=_CONTROL_TIMEOUT_SECONDS),
        )
        return raw["logs"]

    def stats(self, ref: str) -> dict[str, object]:
        """One-shot CPU and memory stats for the dashboard."""
        return cast(
            "dict[str, object]",
            self._request("GET", f"/v1/containers/{ref}/stats", timeout=_CONTROL_TIMEOUT_SECONDS),
        )

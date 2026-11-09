"""Ten JSON endpoints over a unix socket; foreign containers answer a uniform 404."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TypeVar, cast

import anyio
import anyio.to_thread
import requests
from docker.models.containers import Container
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import docker
from docker import errors as docker_errors
from openscientist_launcher.config import LauncherConfig
from openscientist_launcher.images import image_available
from openscientist_launcher.templates import (
    MARKER_LABEL,
    agent_run_kwargs,
    executor_run_kwargs,
)
from openscientist_launcher.validation import (
    ValidationError,
    validate_agent_request,
    validate_exec_request,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_NOT_FOUND_MESSAGE = "container not found"
_STOP_TIMEOUT_CAP_SECONDS = 60
_STOP_TIMEOUT_DEFAULT_SECONDS = 10
#: Threads for the calls that block until a container exits, executor runs
#: and waits. Every other call runs on anyio's default limiter, so a burst of
#: long calls never leaves health, list or stop without a thread.
_LONG_CALL_THREADS = 64
#: Connections the shared Docker client keeps, one for every thread that can
#: hold a call at once.
_DOCKER_POOL_SIZE = _LONG_CALL_THREADS + 40


class ApiError(Exception):
    """A failure the API answers with a status, a stable code and a message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _not_found(ref: str) -> ApiError:
    return ApiError(404, "not_found", _NOT_FOUND_MESSAGE)


def _bad_request(detail: str) -> ApiError:
    return ApiError(400, "bad_request", detail)


def _image_missing(image: str) -> ApiError:
    return ApiError(409, "image_missing", f"image {image} is not available")


def _docker_error(exc: BaseException) -> ApiError:
    return ApiError(502, "docker_error", str(exc))


def _guard(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Run one docker call, translating its exceptions to API errors."""

    def wrapper(*args: object, **kwargs: object) -> _T:
        try:
            return fn(*args, **kwargs)
        except docker_errors.NotFound as exc:
            raise _not_found("") from exc
        except docker_errors.ImageNotFound as exc:
            raise _image_missing("the requested image") from exc
        except docker_errors.DockerException as exc:
            raise _docker_error(exc) from exc

    return wrapper


def _resolve_network(client: docker.DockerClient, configured: str | None) -> str:
    """The launcher's own network, or auto-detected from the launcher container."""
    if configured:
        return configured
    try:
        hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        container = client.containers.get(hostname)
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        for name in networks:
            if name != "bridge":
                return str(name)
    except (docker_errors.DockerException, OSError):
        logger.warning("Auto-detecting the launcher network failed; using bridge")
    return "bridge"


class Server:
    """The state every route handler needs: config, docker client, marker.

    Its methods block on the Docker daemon and run in worker threads. The one
    client is shared by them, since each docker-py call builds its own request
    and the unix socket adapter hands out pooled connections under a lock."""

    def __init__(self, cfg: LauncherConfig) -> None:
        self.cfg = cfg
        self.client = docker.from_env(max_pool_size=_DOCKER_POOL_SIZE)

    def _resolve(self, ref: str) -> Container:
        """The container behind an id or name, or a uniform 404."""
        container = _guard(self.client.containers.get)(ref)
        if container.labels.get(MARKER_LABEL) != self.cfg.instance_id:
            raise _not_found(ref)
        return container

    # -- agent -----------------------------------------------------------

    def create_agent(self, body: object) -> dict[str, object]:
        launch = self._validated(lambda: validate_agent_request(body, self.cfg))
        if not image_available(self.client, self.cfg.agent_image, self.cfg.agent_image_digests):
            raise _image_missing(self.cfg.agent_image)
        network = _resolve_network(self.client, self.cfg.network)
        container = _create_started(
            self.client, agent_run_kwargs(self.cfg, launch, network=network)
        )
        return {
            "id": container.id,
            "short_id": container.short_id,
            "name": container.name,
            "status": container.status,
        }

    def run_exec(self, body: object) -> dict[str, object]:
        req = self._validated(lambda: validate_exec_request(body, self.cfg))
        if not image_available(
            self.client, self.cfg.executor_image, self.cfg.executor_image_digests
        ):
            raise _image_missing(self.cfg.executor_image)
        network = _resolve_network(self.client, self.cfg.network)
        container = _create_started(
            self.client, executor_run_kwargs(self.cfg, req, network=network)
        )
        exit_code: int | None
        try:
            outcome: object = _guard(container.wait)(timeout=req.timeout_seconds)
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            if not _wait_timed_out(exc):
                raise
            # Cut at the wall-clock bound; the SIGKILL stop signal does the rest.
            exit_code = None
            try:
                _guard(container.kill)()
            except docker_errors.NotFound:
                pass
        else:
            code = outcome.get("StatusCode") if isinstance(outcome, dict) else None
            exit_code = code if isinstance(code, int) else 1
        raw = cast("bytes", _guard(container.logs)(stdout=True, stderr=True, stream=False))
        stdout = raw.decode("utf-8", "replace")
        _remove_forced(self.client, container)
        return {"stdout": stdout, "exit_code": exit_code}

    # -- container-scoped --------------------------------------------------

    def inspect(self, ref: str) -> dict[str, object]:
        container = self._resolve(ref)
        _guard(container.reload)()
        state = container.attrs.get("State", {})
        exit_code = None if state.get("Running") else state.get("ExitCode")
        return {"status": container.status, "exit_code": exit_code}

    def stop(self, ref: str, raw_body: bytes) -> dict[str, object]:
        container = self._resolve(ref)
        body = _parse_json(raw_body)
        timeout = _wait_timeout(
            body, default=_STOP_TIMEOUT_DEFAULT_SECONDS, cap=_STOP_TIMEOUT_CAP_SECONDS
        )
        _guard(container.stop)(timeout=timeout)
        return {}

    def remove(self, ref: str) -> dict[str, object]:
        container = self._resolve(ref)
        _remove_forced(self.client, container)
        return {}

    def logs(self, ref: str) -> dict[str, object]:
        container = self._resolve(ref)
        raw = cast("bytes", _guard(container.logs)(stdout=True, stderr=True, stream=False))
        return {"logs": raw.decode("utf-8", "replace")}

    def wait(self, ref: str, raw_body: bytes) -> dict[str, object]:
        container = self._resolve(ref)
        body = _parse_json(raw_body)
        timeout = _wait_timeout(body, default=None, cap=None)
        try:
            outcome: object = _guard(container.wait)(timeout=timeout)
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            if not _wait_timed_out(exc):
                raise
            return {"exit_code": None, "timeout": True}
        code = outcome.get("StatusCode") if isinstance(outcome, dict) else None
        exit_code = code if isinstance(code, int) else 1
        return {"exit_code": exit_code, "timeout": False}

    def stats(self, ref: str) -> object:
        container = self._resolve(ref)
        return _guard(container.stats)(stream=False)

    def list(self, labels: list[str]) -> list[dict[str, object]]:
        """Launcher-created containers matching every label, AND-combined."""
        filters = {"label": [f"{MARKER_LABEL}={self.cfg.instance_id}", *labels]}
        containers = _guard(self.client.containers.list)(all=True, filters=filters)
        entries: list[dict[str, object]] = []
        for container in containers:
            entries.append(
                {
                    "id": container.id,
                    "name": container.name,
                    "status": container.status,
                    "created": container.attrs.get("Created", ""),
                    "labels": {
                        key: value
                        for key, value in container.labels.items()
                        if key.startswith("openscientist.")
                    },
                }
            )
        return entries

    # -- health -------------------------------------------------------------

    def health(self) -> dict[str, object]:
        try:
            _guard(self.client.ping)()
            docker_up = True
        except (docker_errors.DockerException, requests.exceptions.RequestException):
            docker_up = False
        images = {
            "agent": self._image_ok(self.cfg.agent_image, self.cfg.agent_image_digests)
            if docker_up
            else False,
            "executor": self._image_ok(self.cfg.executor_image, self.cfg.executor_image_digests)
            if docker_up
            else False,
        }
        return {"docker": docker_up, "images": images}

    def _image_ok(self, image: str, digests: frozenset[str]) -> bool:
        try:
            return image_available(self.client, image, digests)
        except (docker_errors.DockerException, requests.exceptions.RequestException):
            return False

    @staticmethod
    def _validated(fn: Callable[[], _T]) -> _T:
        try:
            return fn()
        except ValidationError as exc:
            raise _bad_request(str(exc)) from exc


def _wait_timeout(body: object, *, default: int | None, cap: int | None) -> int:
    value = body.get("timeout_seconds") if isinstance(body, dict) else None
    if value is None:
        if default is None:
            raise _bad_request("timeout_seconds is required")
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _bad_request("timeout_seconds must be a positive integer")
    if cap is not None and value > cap:
        raise _bad_request(f"timeout_seconds must be at most {cap}")
    timeout: int = value
    return timeout


def _wait_timed_out(exc: BaseException) -> bool:
    # The unix socket adapter surfaces a read timeout as a ConnectionError.
    return isinstance(exc, requests.exceptions.ReadTimeout) or (
        isinstance(exc, requests.exceptions.ConnectionError) and "timed out" in str(exc)
    )


def _create_started(client: docker.DockerClient, kwargs: dict[str, object]) -> Container:
    """Create a container and start it, removing it again when it does not
    start, so an image the daemon cannot run leaves no container behind."""
    container = cast(Container, _guard(client.containers.create)(**kwargs))
    try:
        _guard(container.start)()
    except BaseException:
        _remove_forced(client, container)
        raise
    return container


def _remove_forced(client: docker.DockerClient, container: Container) -> None:
    try:
        _guard(container.remove)(force=True)
    except docker_errors.NotFound:
        pass


def _parse_json(raw: bytes) -> object:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _bad_request("the request body is not valid JSON") from exc


async def _json_body(request: Request) -> object:
    return _parse_json(await request.body())


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        raise exc
    return JSONResponse(status_code=exc.status, content={"error": f"{exc.code}: {exc.message}"})


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    # A path no route matches is a not-found, like every other miss.
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    return JSONResponse(status_code=exc.status_code, content={"error": f"not_found: {exc.detail}"})


async def _internal(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled launcher error on %s", request.url.path)
    return JSONResponse(
        status_code=500, content={"error": "internal: the launcher hit an unexpected error"}
    )


def create_app(cfg: LauncherConfig) -> Starlette:
    server = Server(cfg)
    long_calls = anyio.CapacityLimiter(_LONG_CALL_THREADS)

    async def blocking(fn: Callable[..., _T], *args: object) -> _T:
        return await anyio.to_thread.run_sync(partial(fn, *args))

    async def long_blocking(fn: Callable[..., _T], *args: object) -> _T:
        return await anyio.to_thread.run_sync(partial(fn, *args), limiter=long_calls)

    async def agent_create(request: Request) -> JSONResponse:
        body = await _json_body(request)
        return JSONResponse(await blocking(server.create_agent, body))

    async def exec_run(request: Request) -> JSONResponse:
        body = await _json_body(request)
        return JSONResponse(await long_blocking(server.run_exec, body))

    async def list_containers(request: Request) -> JSONResponse:
        labels = request.query_params.getlist("label")
        return JSONResponse(await blocking(server.list, labels))

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(await blocking(server.health))

    async def inspect_container(request: Request) -> JSONResponse:
        return JSONResponse(await blocking(server.inspect, request.path_params["ref"]))

    async def stop_container(request: Request) -> JSONResponse:
        raw = await request.body()
        return JSONResponse(await blocking(server.stop, request.path_params["ref"], raw))

    async def remove_container(request: Request) -> JSONResponse:
        return JSONResponse(await blocking(server.remove, request.path_params["ref"]))

    async def wait_container(request: Request) -> JSONResponse:
        raw = await request.body()
        return JSONResponse(await long_blocking(server.wait, request.path_params["ref"], raw))

    async def logs_container(request: Request) -> JSONResponse:
        return JSONResponse(await blocking(server.logs, request.path_params["ref"]))

    async def stats_container(request: Request) -> JSONResponse:
        return JSONResponse(await blocking(server.stats, request.path_params["ref"]))

    app = Starlette(
        routes=[
            Route("/v1/containers/agent", agent_create, methods=["POST"]),
            Route("/v1/exec", exec_run, methods=["POST"]),
            Route("/v1/containers", list_containers, methods=["GET"]),
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/containers/{ref}", inspect_container, methods=["GET"]),
            Route("/v1/containers/{ref}/stop", stop_container, methods=["POST"]),
            Route("/v1/containers/{ref}/remove", remove_container, methods=["POST"]),
            Route("/v1/containers/{ref}/wait", wait_container, methods=["POST"]),
            Route("/v1/containers/{ref}/logs", logs_container, methods=["GET"]),
            Route("/v1/containers/{ref}/stats", stats_container, methods=["GET"]),
        ],
        exception_handlers={
            ApiError: _api_error,
            StarletteHTTPException: _http_error,
            Exception: _internal,
        },
    )
    return app

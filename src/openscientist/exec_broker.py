"""Web-side execution broker: an authenticated internal listener wrapping
ContainerManager.execute_code() so a job container runs code without the
Docker socket. Accepts only execute_code parameters, never a container spec.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from openscientist.admin_settings import current_admin_settings
from openscientist.budget import check_budget
from openscientist.container_manager import ContainerManager, get_container_manager
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import get_admin_session
from openscientist.exec_broker_client import EXEC_TOKEN_HEADER
from openscientist.job_container.secrets import broker_accepts
from openscientist.job_container.utils import HostPathSettings, to_container_path
from openscientist.job_manager import _effective_model
from openscientist.listener import bind_listener
from openscientist.request_body import read_request_body
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

_SUPPORTED_LANGUAGES = frozenset({"python", "rust", "sparql"})
_EXEC_REQUEST_LIMIT = 1024 * 1024
_BUDGET_REQUEST_LIMIT = 4096

# Rust execution is compile-bound, so its cap never undercuts the tool's window.
_RUST_MIN_TIMEOUT_SECONDS = 300

# The executor runs untrusted code, so the result it hands back stays small.
_RESULT_FIELD_LIMIT = 10_000
_TRUNCATED_NOTE = "\n[...truncated]"
_PLOT_LIMIT = 50


def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
    """Cap the untrusted result's string fields and plot list in place."""
    for field in ("output", "error", "traceback"):
        value = result.get(field)
        if isinstance(value, str) and len(value) > _RESULT_FIELD_LIMIT:
            keep = _RESULT_FIELD_LIMIT - len(_TRUNCATED_NOTE)
            result[field] = value[:keep].rstrip() + _TRUNCATED_NOTE
    plots = result.get("plots")
    if isinstance(plots, list) and len(plots) > _PLOT_LIMIT:
        result["plots"] = plots[:_PLOT_LIMIT]
    return result


class _PathConfinementError(ValueError):
    """A requested path resolves outside the authenticated job's directory."""


def _job_container_root(cs: HostPathSettings, job_id: str) -> Path:
    """The web-container path of the job's own directory."""
    return (Path(cs.container_app_dir) / "jobs" / job_id).resolve()


def _confined_container_path(host_path: str, cs: HostPathSettings, job_root: Path) -> Path:
    """Map a host path to the web-container path, rejecting anything outside the job dir."""
    container_path = to_container_path(Path(host_path), cs).resolve()
    if container_path != job_root and job_root not in container_path.parents:
        raise _PathConfinementError(f"path {host_path!r} is outside job directory {job_root}")
    return container_path


def create_exec_broker_app(*, manager: Callable[[], ContainerManager]) -> Starlette:
    """Build the broker ASGI app. The manager is resolved per request."""
    executions: dict[str, set[asyncio.Task[Response]]] = {}

    async def handler(request: Request) -> Response:
        presented = request.headers.get(EXEC_TOKEN_HEADER, "")
        if not presented or not broker_accepts(presented.rpartition(".")[0], presented):
            return Response("unauthorized", status_code=401)
        payload = await read_request_body(request, limit=_EXEC_REQUEST_LIMIT)
        try:
            raw: object = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(raw, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        body: dict[str, Any] = raw

        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return JSONResponse({"error": "missing job_id"}, status_code=400)

        if not presented or not broker_accepts(job_id, presented):
            return Response("unauthorized", status_code=401)

        language = body.get("language", "python")
        if language not in _SUPPORTED_LANGUAGES:
            return JSONResponse({"error": f"unsupported language {language!r}"}, status_code=400)
        code = body.get("code")
        if not isinstance(code, str):
            return JSONResponse({"error": "missing code"}, status_code=400)
        description = body.get("description", "")
        if not isinstance(description, str):
            return JSONResponse({"error": "description must be a string"}, status_code=400)
        output_dir_raw = body.get("output_dir")
        if not isinstance(output_dir_raw, str) or not output_dir_raw:
            return JSONResponse({"error": "missing output_dir"}, status_code=400)
        try:
            iteration = int(body.get("iteration", 0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "iteration must be an integer"}, status_code=400)

        raw_timeout = body.get("timeout")
        if raw_timeout is None:
            timeout: int | None = None
        elif (
            isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, (int, float))
            or (isinstance(raw_timeout, float) and not math.isfinite(raw_timeout))
        ):
            return JSONResponse(
                {"error": "timeout must be a positive number of seconds"},
                status_code=422,
            )
        elif int(raw_timeout) <= 0:
            return JSONResponse(
                {"error": "timeout must be a positive number of seconds"},
                status_code=422,
            )
        else:
            try:
                admin = await current_admin_settings()
            except Exception:
                logger.exception("Failed to read the admin settings for job %s", job_id)
                return Response("the admin settings could not be read", status_code=500)
            cap = admin.executor_timeout_seconds
            if language == "rust":
                cap = max(cap, _RUST_MIN_TIMEOUT_SECONDS)
            timeout = min(int(raw_timeout), cap)

        data_path_raw = body.get("data_path")
        data_files_raw = body.get("data_files", [])
        if not isinstance(data_files_raw, list):
            return JSONResponse({"error": "data_files must be a list"}, status_code=400)

        cs = get_settings().container
        job_root = _job_container_root(cs, job_id)
        try:
            output_dir = _confined_container_path(output_dir_raw, cs, job_root)
            data_path: Path | None = None
            if isinstance(data_path_raw, str) and data_path_raw:
                data_path = _confined_container_path(data_path_raw, cs, job_root)
            data_files: list[dict[str, Any]] = []
            for entry in data_files_raw:
                if not isinstance(entry, dict):
                    return JSONResponse(
                        {"error": "data_files entries must be objects"}, status_code=400
                    )
                raw_path = entry.get("path", "")
                if raw_path:
                    confined = _confined_container_path(raw_path, cs, job_root)
                    data_files.append({**entry, "path": str(confined)})
                else:
                    data_files.append(dict(entry))
        except _PathConfinementError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

        active = executions.setdefault(job_id, set())
        if len(active) >= cs.executor_max_concurrent_per_job:
            return JSONResponse(
                {"error": "job concurrent execution limit reached, retry after an execution ends"},
                status_code=429,
            )

        async def execute() -> Response:
            result = await asyncio.to_thread(
                manager().execute_code,
                code=code,
                job_id=job_id,
                data_path=str(data_path) if data_path is not None else None,
                output_dir=output_dir,
                timeout=timeout,
                description=description,
                iteration=iteration,
                data_files=data_files,
                language=language,
            )
            return JSONResponse(_bounded_result(result))

        def finished(task: asyncio.Task[Response]) -> None:
            active.discard(task)
            if not active:
                executions.pop(job_id, None)
            if not task.cancelled():
                task.exception()

        task = asyncio.create_task(execute())
        active.add(task)
        task.add_done_callback(finished)
        # A disconnected caller still holds its slot until the container exits.
        return await asyncio.shield(task)

    async def budget_handler(request: Request) -> Response:
        presented = request.headers.get(EXEC_TOKEN_HEADER, "")
        if not presented or not broker_accepts(presented.rpartition(".")[0], presented):
            return Response("unauthorized", status_code=401)
        payload = await read_request_body(request, limit=_BUDGET_REQUEST_LIMIT)
        try:
            raw: object = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(raw, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        body: dict[str, Any] = raw

        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return JSONResponse({"error": "missing job_id"}, status_code=400)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return JSONResponse({"error": "missing request_id"}, status_code=400)

        if not presented or not broker_accepts(job_id, presented):
            return Response("unauthorized", status_code=401)

        try:
            job_uuid = UUID(job_id)
        except ValueError:
            return JSONResponse({"error": "job_id must be a UUID"}, status_code=400)
        async with get_admin_session() as session:
            job = await session.get(JobModel, job_uuid)
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)

        try:
            verdict = await check_budget(
                job_id=job_uuid,
                provider_id=job.llm_provider,
                model=(job.llm_config or {}).get("model")
                or _effective_model(get_settings().provider),
                include_unpriced=False,
            )
        except Exception:
            # The server's own 500 for an internal error, which the run retries.
            logger.exception("Budget check failed for job %s", job_id)
            return Response("the budget check failed", status_code=500)
        return JSONResponse(
            {
                "request_id": request_id,
                "ok": verdict.ok,
                "limit": verdict.limit,
                "message": verdict.message,
            }
        )

    return Starlette(
        routes=[
            Route("/execute", handler, methods=["POST"]),
            Route("/budget", budget_handler, methods=["POST"]),
        ]
    )


class _NoSignalServer(uvicorn.Server):
    """A second server sharing the loop must not install signal handlers."""

    def install_signal_handlers(self) -> None:
        return None


_broker_server: uvicorn.Server | None = None
_broker_task: asyncio.Task[None] | None = None


async def start_exec_broker() -> None:
    """Start the broker listener as a loop task. The single, always-on exec path.

    Raises:
        ListenerBindError: Its port cannot be bound.
    """
    global _broker_server, _broker_task
    if _broker_task is not None:
        return
    port = get_settings().container.exec_broker_port
    sock = bind_listener(port, name="execution broker", setting="OPENSCIENTIST_EXEC_BROKER_PORT")
    app = create_exec_broker_app(manager=get_container_manager)
    config = uvicorn.Config(app, log_level="warning")
    _broker_server = _NoSignalServer(config)
    _broker_task = asyncio.create_task(_broker_server.serve(sockets=[sock]))
    logger.info("Execution broker listening on port %d", port)

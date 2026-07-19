"""Web-side execution broker: an authenticated internal listener wrapping
ContainerManager.execute_code() so a job container runs code without the
Docker socket. Accepts only execute_code parameters, never a container spec.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from openscientist.container_manager import ContainerManager, get_container_manager
from openscientist.exec_broker_client import EXEC_BROKER_PORT, EXEC_TOKEN_HEADER
from openscientist.job_container.secrets import make_exec_placeholder
from openscientist.job_container.utils import HostPathSettings, to_container_path
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

_SUPPORTED_LANGUAGES = frozenset({"python", "rust", "sparql"})


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


def create_exec_broker_app(
    *,
    master_key: Callable[[], str],
    manager: Callable[[], ContainerManager],
) -> Starlette:
    """Build the broker ASGI app. The callables are resolved per request."""

    async def handler(request: Request) -> Response:
        presented = request.headers.get(EXEC_TOKEN_HEADER, "")
        try:
            raw: Any = await request.json()
        except Exception:
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(raw, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        body: dict[str, Any] = raw

        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return JSONResponse({"error": "missing job_id"}, status_code=400)

        # Recompute the token from the claimed job_id and constant-time compare.
        expected = make_exec_placeholder(master_key(), job_id)
        if not presented or not hmac.compare_digest(expected, presented):
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
            timeout_raw = body.get("timeout")
            timeout = int(timeout_raw) if timeout_raw else None
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "iteration and timeout must be integers"}, status_code=400
            )

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

        # execute_code() blocks on a container, so run it off the event loop.
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
        return JSONResponse(result)

    return Starlette(routes=[Route("/execute", handler, methods=["POST"])])


class _NoSignalServer(uvicorn.Server):
    """A second server sharing the loop must not install signal handlers."""

    def install_signal_handlers(self) -> None:
        return None


_broker_server: uvicorn.Server | None = None
_broker_task: asyncio.Task[None] | None = None


async def start_exec_broker() -> None:
    """Start the broker listener as a loop task. The single, always-on exec path."""
    global _broker_server, _broker_task
    if _broker_task is not None:
        return
    app = create_exec_broker_app(
        master_key=lambda: get_settings().secret_key,
        manager=get_container_manager,
    )
    config = uvicorn.Config(app, host="0.0.0.0", port=EXEC_BROKER_PORT, log_level="warning")
    _broker_server = _NoSignalServer(config)
    _broker_task = asyncio.create_task(_broker_server.serve())
    logger.info("Execution broker listening on port %d", EXEC_BROKER_PORT)

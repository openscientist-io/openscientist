"""A2A 1.0 transport over the existing authenticated job manager."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from a2a.server.jsonrpc_models import JSONParseError
from a2a.server.request_handlers.response_helpers import build_error_response
from a2a.types import a2a_pb2 as wire
from a2a.utils import errors
from a2a.utils.proto_utils import validate_proto_required_fields
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from google.protobuf.message import Message as ProtoMessage
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.api.auth import get_current_user_from_api_key
from openscientist.api.endpoints.jobs import _get_job_manager
from openscientist.async_tasks import create_background_task
from openscientist.database.models import A2ASettings, A2ATask, Administrator, Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_admin_session, get_session_ctx
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["A2A"])
CURRENT_USER = Depends(get_current_user_from_api_key)
_STATES = {
    "pending": wire.TASK_STATE_SUBMITTED,
    "queued": wire.TASK_STATE_SUBMITTED,
    "running": wire.TASK_STATE_WORKING,
    "generating_report": wire.TASK_STATE_WORKING,
    "awaiting_feedback": wire.TASK_STATE_INPUT_REQUIRED,
    "completed": wire.TASK_STATE_COMPLETED,
    "failed": wire.TASK_STATE_FAILED,
    "cancelled": wire.TASK_STATE_CANCELED,
}
_TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}
_METHODS = {
    "SendMessage": wire.SendMessageRequest,
    "GetTask": wire.GetTaskRequest,
    "ListTasks": wire.ListTasksRequest,
    "CancelTask": wire.CancelTaskRequest,
}
_UNSUPPORTED = {
    "SendStreamingMessage",
    "SubscribeToTask",
    "GetExtendedAgentCard",
    "CreateTaskPushNotificationConfig",
    "GetTaskPushNotificationConfig",
    "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
}


class Envelope(BaseModel):
    """Strict JSON-RPC envelope; SDK types validate protocol payloads."""

    model_config = ConfigDict(extra="forbid", strict=True)
    jsonrpc: str
    id: str | int
    method: str
    params: dict[str, Any] = {}


class EnabledBody(BaseModel):
    """The sole deployment-wide A2A setting."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


async def service_status() -> dict[str, Any]:
    """Read the shared switch without exposing any user or job metadata."""
    async with get_session_ctx() as session:
        setting = await session.get(A2ASettings, 1)
        if setting is None:
            raise HTTPException(503, "A2A settings unavailable; apply database migrations")
        base = get_settings().auth.app_url.rstrip("/")
        return {
            "enabled": setting.enabled,
            "agent_card_url": f"{base}/.well-known/agent-card.json",
            "rpc_url": f"{base}/a2a",
        }


async def change_enabled(user_id: UUID, enabled: bool) -> dict[str, Any]:
    """Recheck administrator membership on every API or UI mutation."""
    async with get_admin_session() as session:
        user = await session.get(User, user_id)
        admin = await session.get(Administrator, user_id)
        if user is None or not user.is_active or admin is None:
            raise HTTPException(403, "Only administrators can change A2A settings")
        setting = await session.get(A2ASettings, 1)
        if setting is None:
            raise HTTPException(503, "A2A settings unavailable; apply database migrations")
        setting.enabled = enabled
        await session.commit()
    return await service_status()


@router.get("/api/v1/a2a/settings")
async def read_settings(response: Response, user: User = CURRENT_USER) -> dict[str, Any]:
    """Show authenticated clients the same status and URLs as the UI."""
    response.headers["Cache-Control"] = "no-store"
    return await service_status()


@router.put("/api/v1/a2a/settings")
async def write_settings(
    body: EnabledBody, response: Response, user: User = CURRENT_USER
) -> dict[str, Any]:
    """Allow an authenticated administrator to enable or disable admission."""
    response.headers["Cache-Control"] = "no-store"
    return await change_enabled(user.id, body.enabled)


async def require_enabled(response: Response) -> None:
    """Gate discovery and all RPCs, even when clients cache discovery."""
    response.headers["Cache-Control"] = "no-store"
    if not (await service_status())["enabled"]:
        raise HTTPException(503, "A2A is turned off", headers={"Cache-Control": "no-store"})


@router.get("/.well-known/agent-card.json", dependencies=[Depends(require_enabled)])
async def agent_card() -> dict[str, Any]:
    """Publish one generic agent with the existing API-key security scheme."""
    card = wire.AgentCard(
        name="openscientist",
        version="0.1.0",
        description="Run a task through the configured OpenScientist agent and job manager.",
        supported_interfaces=[
            wire.AgentInterface(
                url=f"{get_settings().auth.app_url.rstrip('/')}/a2a",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        capabilities=wire.AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            wire.AgentSkill(
                id="execute-task",
                name="Execute task",
                tags=["general"],
                description="Submit a text task and retrieve its result.",
            )
        ],
    )
    result: dict[str, Any] = MessageToDict(card, always_print_fields_with_no_presence=True)
    result["securitySchemes"] = {"apiKey": {"httpAuthSecurityScheme": {"scheme": "bearer"}}}
    # JSON scope values are arrays (Go SDK wire format), not the raw
    # protobuf StringList wrapper. Bearer credentials require no scopes.
    result["securityRequirements"] = [{"schemes": {"apiKey": []}}]
    return result


def _report(job: Job) -> str:
    """Read only the owner's final Markdown report, with a bounded response."""
    root = _get_job_manager().jobs_dir.resolve()
    job_dir = root / str(job.id)
    report = job_dir / "final_report.md"
    if report.exists() and job_dir.resolve() == job_dir and report.resolve().parent == job_dir:
        with report.open(encoding="utf-8") as handle:
            content = handle.read(1_000_001)
        if len(content) > 1_000_000:
            content = (
                content[:1_000_000] + "\n[Report truncated; open the job for the full report.]"
            )
        return content
    return job.result_summary or job.consensus_answer or ""


async def _project(session: AsyncSession, row: A2ATask, job: Job) -> dict[str, Any]:
    if row.final_task is not None:
        return deepcopy(row.final_task)
    job_url = f"{get_settings().auth.app_url.rstrip('/')}/job/{job.id}"
    state = _STATES.get(job.status, wire.TASK_STATE_WORKING)
    text = f"Job {job.status}. Open {job_url}"
    if state == wire.TASK_STATE_INPUT_REQUIRED:
        text = f"Input required. Open {job_url} to reply in OpenScientist."
    if state == wire.TASK_STATE_FAILED:
        text = f"Job failed. Open {job_url} for details."
    task = wire.Task(id=str(job.id), context_id=str(job.id), status=wire.TaskStatus(state=state))
    task.status.timestamp.FromDatetime(job.updated_at)
    task.status.message.CopyFrom(
        wire.Message(
            message_id=f"{job.id}-status",
            role=wire.ROLE_AGENT,
            parts=[wire.Part(text=text)],
        )
    )
    task.history.add().CopyFrom(ParseDict(row.message, wire.Message()))
    if state == wire.TASK_STATE_COMPLETED:
        output = await asyncio.to_thread(_report, job)
        if output:
            task.artifacts.add(
                artifact_id=f"{job.id}-result", name="Result", parts=[wire.Part(text=output)]
            )
            task.history.add(
                message_id=f"{job.id}-result",
                role=wire.ROLE_AGENT,
                parts=[wire.Part(text=output)],
                task_id=str(job.id),
                context_id=str(job.id),
            )
    result = MessageToDict(task)
    if result["status"]["state"] in _TERMINAL:
        row.final_task = result
        await session.commit()
        # A commit may release the connection carrying the RLS context.
        await set_current_user(session, job.owner_id)
    return deepcopy(result)


async def get_task(user_id: UUID, task_id: str) -> dict[str, Any]:
    """Only expose A2A jobs owned by this user, including when jobs are shared."""
    try:
        job_id = UUID(task_id)
    except ValueError as exc:
        raise errors.TaskNotFoundError() from exc
    async with get_session_ctx() as session:
        await set_current_user(session, user_id)
        result = await session.execute(
            select(A2ATask, Job)
            .join(Job, A2ATask.job_id == Job.id)
            .where(A2ATask.job_id == job_id, Job.owner_id == user_id)
            .with_for_update(of=A2ATask)
        )
        pair = result.one_or_none()
        if pair is None:
            raise errors.TaskNotFoundError()
        return await _project(session, pair[0], pair[1])


def _history(task: dict[str, Any], length: int | None) -> dict[str, Any]:
    if length is not None:
        if length == 0:
            task.pop("history", None)
        else:
            task["history"] = task.get("history", [])[-length:]
    return task


async def _submit(user_id: UUID, message: wire.Message, prompt: str) -> dict[str, Any]:
    manager = _get_job_manager()
    job_id = uuid4()
    # Use exactly the normal UI/JobManager defaults and capture its configured
    # provider/model. No A2A-specific harness or research workflow exists.
    await asyncio.to_thread(
        manager.create_job,
        job_id=str(job_id),
        research_question=prompt,
        data_files=[],
        auto_start=False,
        owner_id=str(user_id),
    )
    async with get_session_ctx() as session:
        await set_current_user(session, user_id)
        session.add(A2ATask(job_id=job_id, message=MessageToDict(message)))
        await session.commit()
    await asyncio.to_thread(manager.start_job, str(job_id))
    return await get_task(user_id, str(job_id))


async def send(params: wire.SendMessageRequest, user: User) -> dict[str, Any]:
    if not user.is_approved:
        raise HTTPException(403, "Administrator approval is required to start jobs")
    message = params.message
    config = params.configuration
    if message.role != wire.ROLE_USER:
        raise errors.InvalidParamsError("Only user messages are accepted")
    if message.task_id or message.context_id:
        raise errors.UnsupportedOperationError(
            "Use the job UI for follow-ups; omit taskId/contextId for a new job"
        )
    if message.extensions or message.reference_task_ids:
        raise errors.UnsupportedOperationError("Extensions and task references are unsupported")
    if config.HasField("task_push_notification_config"):
        raise errors.PushNotificationNotSupportedError()
    if config.accepted_output_modes and "text/plain" not in config.accepted_output_modes:
        raise errors.ContentTypeNotSupportedError()
    if config.HasField("history_length") and config.history_length < 0:
        raise errors.InvalidParamsError("historyLength must not be negative")
    if not message.parts or any(
        p.WhichOneof("content") != "text" or p.media_type not in {"", "text/plain"}
        for p in message.parts
    ):
        raise errors.ContentTypeNotSupportedError("Only text parts are supported")
    prompt = "\n".join(p.text for p in message.parts)
    if not prompt.strip():
        raise errors.InvalidParamsError("Message must not be blank")
    submission = create_background_task(
        _submit(user.id, message, prompt), name="a2a-submit", logger=logger
    )
    # Disconnecting the caller must not interrupt an accepted job submission.
    task = await asyncio.shield(submission)
    while not config.return_immediately and task["status"]["state"] not in _TERMINAL | {
        "TASK_STATE_INPUT_REQUIRED"
    }:
        await asyncio.sleep(1)
        task = await get_task(user.id, task["id"])
    length = config.history_length if config.HasField("history_length") else None
    return {"task": _history(task, length)}


async def dispatch(params: ProtoMessage, user: User) -> dict[str, Any]:
    if isinstance(params, wire.SendMessageRequest):
        return await send(params, user)
    if isinstance(params, wire.GetTaskRequest):
        return _history(
            await get_task(user.id, params.id),
            params.history_length if params.HasField("history_length") else None,
        )
    if isinstance(params, wire.CancelTaskRequest):
        task = await get_task(user.id, params.id)
        if task["status"]["state"] == "TASK_STATE_CANCELED":
            return task
        if task["status"]["state"] in _TERMINAL:
            raise errors.TaskNotCancelableError()
        try:
            await asyncio.to_thread(_get_job_manager().cancel_job, task["id"])
        except ValueError as exc:
            raise errors.TaskNotCancelableError(
                "This job cannot be canceled through the job manager in its current state"
            ) from exc
        return await get_task(user.id, task["id"])
    if isinstance(params, wire.ListTasksRequest):
        size = params.page_size or 50
        if not 1 <= size <= 100:
            raise errors.InvalidParamsError("pageSize must be 1–100")
        if params.page_token and (
            not params.page_token.isascii() or not params.page_token.isdecimal()
        ):
            raise errors.InvalidParamsError("Invalid pageToken")
        offset = int(params.page_token or "0")
        async with get_session_ctx() as session:
            await set_current_user(session, user.id)
            ids = list(
                (
                    await session.scalars(
                        select(A2ATask.job_id)
                        .join(Job)
                        .where(Job.owner_id == user.id)
                        .order_by(Job.created_at.desc(), Job.id.desc())
                    )
                ).all()
            )
        # shortcut: scans this owner's task history; SQL pagination and a
        # stored state index are the upgrade path for large installations.
        tasks = []
        for job_id in ids:
            if params.context_id and params.context_id != str(job_id):
                continue
            task = await get_task(user.id, str(job_id))
            if params.status and task["status"]["state"] != wire.TaskState.Name(params.status):
                continue
            if params.HasField("status_timestamp_after"):
                timestamp = Timestamp()
                timestamp.FromJsonString(task["status"]["timestamp"])
                if timestamp.ToNanoseconds() <= params.status_timestamp_after.ToNanoseconds():
                    continue
            tasks.append(task)
        page = tasks[offset : offset + size]
        for task in page:
            _history(task, params.history_length if params.HasField("history_length") else None)
            if not params.include_artifacts:
                task.pop("artifacts", None)
        return {
            "tasks": page,
            "totalSize": len(tasks),
            "pageSize": size,
            "nextPageToken": str(offset + size) if offset + size < len(tasks) else "",
        }
    raise errors.MethodNotFoundError()


@router.post("/a2a", dependencies=[Depends(require_enabled)])
async def rpc(request: Request, user: User = CURRENT_USER) -> dict[str, Any]:
    """Authenticate before JSON-RPC dispatch, using the existing API key path."""
    rid: str | int | None = None
    try:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return build_error_response(None, JSONParseError())
        try:
            envelope = Envelope.model_validate(body)
        except ValidationError as exc:
            raise errors.InvalidRequestError() from exc
        rid = envelope.id
        if envelope.jsonrpc != "2.0":
            raise errors.InvalidRequestError()
        if request.headers.get("a2a-version", "1.0") != "1.0":
            raise errors.VersionNotSupportedError("This endpoint supports A2A 1.0")
        if request.headers.get("a2a-extensions") or envelope.method in _UNSUPPORTED:
            raise errors.UnsupportedOperationError()
        proto = _METHODS.get(envelope.method)
        if proto is None:
            raise errors.MethodNotFoundError()
        try:
            params = ParseDict(envelope.params, proto())
            validate_proto_required_fields(params)
        except (ParseError, ValueError) as exc:
            raise errors.InvalidParamsError() from exc
        if params.tenant:
            raise errors.InvalidParamsError("This endpoint has no tenants")
        if (
            hasattr(params, "history_length")
            and params.HasField("history_length")
            and params.history_length < 0
        ):
            raise errors.InvalidParamsError("historyLength must not be negative")
        return {"jsonrpc": "2.0", "id": rid, "result": await dispatch(params, user)}
    except errors.A2AError as exc:
        return build_error_response(rid, exc)
    except HTTPException:
        raise
    except Exception:
        logger.exception("A2A request failed")
        return build_error_response(rid, errors.InternalError())

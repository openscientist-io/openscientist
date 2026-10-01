"""A2A wire, ownership and lifecycle tests against real PostgreSQL records."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from a2a.types import a2a_pb2 as wire
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from openscientist.api import a2a
from openscientist.database.models import A2ASettings, A2ATask, APIKey, Job
from openscientist.database.rls import set_current_user
from tests.a2a_fixtures import Environment


def message(prompt: str = "analyze arbitrary data", **extra: Any) -> dict[str, Any]:
    return {
        "message": {
            "messageId": uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"text": prompt}],
            **extra,
        },
        "configuration": {"returnImmediately": True},
    }


async def test_discovery_and_existing_api_key_auth(environment: Environment) -> None:
    env = environment
    discovery = await env.client.get("/.well-known/agent-card.json")
    card = ParseDict(discovery.json(), wire.AgentCard())
    assert card.name == "openscientist"
    assert card.supported_interfaces[0].url == "https://example.org/scientist/a2a"
    assert card.supported_interfaces[0].protocol_version == "1.0"
    assert card.security_schemes["apiKey"].http_auth_security_scheme.scheme == "bearer"
    assert discovery.json()["securityRequirements"] == [{"schemes": {"apiKey": []}}]
    assert "apiKey" in card.security_requirements[0].schemes
    assert not card.capabilities.streaming
    assert discovery.json()["capabilities"] == {"streaming": False, "pushNotifications": False}
    assert discovery.headers["cache-control"] == "no-store"
    env.client.headers.pop("Authorization")
    assert (await env.client.post("/a2a", json={})).status_code in {401, 403}
    assert (await env.client.get("/.well-known/agent-card.json")).status_code == 200
    for token in ["wrong", "client:wrong", env.tokens[0].replace("client:", "other:")]:
        env.client.headers["Authorization"] = f"Bearer {token}"
        assert (await env.client.post("/a2a", json={})).status_code == 401
    async with env.factory() as session:
        key = await session.scalar(select(APIKey).where(APIKey.user_id == env.users[0].id))
        assert key is not None
        key.is_active = False
        await session.commit()
    env.use_user(0)
    assert (await env.client.post("/a2a", json={})).status_code == 401
    assert not env.started


async def test_host_app_registers_authenticated_a2a_routes(environment: Environment) -> None:
    from openscientist.web_app import _register_api_routes

    host = FastAPI()
    _register_api_routes(host)
    async with AsyncClient(transport=ASGITransport(app=host), base_url="http://test") as client:
        assert (await client.get("/.well-known/agent-card.json")).json()["name"] == "openscientist"
        assert (await client.get("/api/v1/a2a/settings")).status_code in {401, 403}
        client.headers["Authorization"] = f"Bearer {environment.tokens[0]}"
        assert (await client.get("/api/v1/a2a/settings")).json()["enabled"]
        result = await client.post(
            "/a2a", json={"jsonrpc": "2.0", "id": "list", "method": "ListTasks"}
        )
        assert result.json()["result"]["tasks"] == []


async def test_blocking_send_observes_committed_job_updates(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A separate DB session can complete a waiting send without local notification."""
    env = environment
    observed_submission = asyncio.Event()
    original_get_task = a2a.get_task

    async def observe(user_id: UUID, task_id: str) -> dict[str, Any]:
        task = await original_get_task(user_id, task_id)
        if task["status"]["state"] == "TASK_STATE_SUBMITTED":
            observed_submission.set()
        return task

    monkeypatch.setattr(a2a, "get_task", observe)
    params = message()
    params["configuration"] = {"historyLength": 1}
    async with asyncio.timeout(10):
        pending = asyncio.create_task(env.rpc("SendMessage", params))
        try:
            await observed_submission.wait()
            assert not pending.done()
            task_id = env.started[-1]
            (env.manager.jobs_dir / task_id / "final_report.md").write_text("Completed elsewhere")
            await env.state(task_id, "completed")
            task = (await pending)["result"]["task"]
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(task["history"]) == 1
    assert task["artifacts"][0]["parts"] == [{"text": "Completed elsewhere"}]


@pytest.mark.parametrize("report_kind", ["oversized", "symlink", "summary"])
async def test_report_bounds_and_external_file_isolation(
    environment: Environment, report_kind: str
) -> None:
    env = environment
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    task_id = task["id"]
    report = env.manager.jobs_dir / task_id / "final_report.md"
    if report_kind == "oversized":
        report.write_text("a" * 1_000_010)
    elif report_kind == "symlink":
        outside = env.manager.jobs_dir / "other-owner-report.md"
        outside.write_text("Another user's private report")
        report.symlink_to(outside)
    else:
        async with env.factory() as session:
            job = await session.get(Job, UUID(task_id))
            assert job is not None
            job.result_summary = "Stored summary"
            await session.commit()
    await env.state(task_id, "completed")
    result = (await env.rpc("GetTask", {"id": task_id}))["result"]
    if report_kind == "symlink":
        assert "artifacts" not in result
        assert len(result["history"]) == 1
    else:
        output = result["artifacts"][0]["parts"][0]["text"]
        if report_kind == "oversized":
            assert (
                output
                == "a" * 1_000_000 + "\n[Report truncated; open the job for the full report.]"
            )
        else:
            assert output == "Stored summary"


async def test_send_uses_normal_job_manager_and_returns_report(environment: Environment) -> None:
    env = environment
    prompt = "  analyze any\narbitrary data  "
    task = (await env.rpc("SendMessage", message(prompt)))["result"]["task"]
    task_id = task["id"]
    assert task["contextId"] == task_id
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert env.started == [task_id]
    async with env.factory() as session:
        job = await session.get(Job, UUID(task_id))
        assert job is not None
        assert job.research_question == prompt
        assert job.owner_id == env.users[0].id
        assert job.max_iterations == 10 and not job.use_hypotheses
        assert job.investigation_mode == "autonomous"
        assert job.llm_provider == "anthropic" and job.llm_config == {"model": "claude-test-model"}
    listed = (await env.client.get("/api/v1/jobs")).json()
    assert task_id in [j["id"] for j in listed["jobs"]]
    (env.manager.jobs_dir / task_id / "final_report.md").write_text("# Findings\nThe answer.")
    await env.state(task_id, "completed")
    finished = (await env.rpc("GetTask", {"id": task_id}))["result"]
    assert finished["artifacts"][0]["parts"] == [{"text": "# Findings\nThe answer."}]
    assert len(finished["history"]) == 2
    assert (
        "history" not in (await env.rpc("GetTask", {"id": task_id, "historyLength": 0}))["result"]
    )
    assert (
        len((await env.rpc("GetTask", {"id": task_id, "historyLength": 1}))["result"]["history"])
        == 1
    )
    # New DB sessions project a persisted result even if the UI retries the job.
    await env.state(task_id, "running")
    (env.manager.jobs_dir / task_id / "final_report.md").write_text("Changed")
    assert (await env.rpc("GetTask", {"id": task_id}))["result"] == finished


async def test_ownership_approval_and_listing(environment: Environment) -> None:
    env = environment
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    env.use_user(1)
    for method in ["GetTask", "CancelTask"]:
        assert (await env.rpc(method, {"id": task["id"]}))["error"]["code"] == -32001
    assert (await env.rpc("ListTasks"))["result"]["tasks"] == []
    env.use_user(2)
    denied = await env.client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": "denied", "method": "SendMessage", "params": message()},
    )
    assert denied.status_code == 403
    env.use_user(0)
    other = (await env.rpc("SendMessage", message("second")))["result"]["task"]
    page = (await env.rpc("ListTasks", {"pageSize": 1}))["result"]
    assert page["totalSize"] == 2 and len(page["tasks"]) == 1 and page["nextPageToken"]
    page2 = (await env.rpc("ListTasks", {"pageSize": 1, "pageToken": page["nextPageToken"]}))[
        "result"
    ]
    assert page2["tasks"][0]["id"] != page["tasks"][0]["id"]
    assert (await env.rpc("ListTasks", {"contextId": other["id"]}))["result"]["totalSize"] == 1
    assert (await env.rpc("ListTasks", {"statusTimestampAfter": datetime.now(UTC).isoformat()}))[
        "result"
    ]["tasks"] == []
    assert (await env.rpc("ListTasks", {"status": "TASK_STATE_COMPLETED"}))["result"]["tasks"] == []


async def test_toggle_is_admin_only_durable_and_preserves_jobs(environment: Environment) -> None:
    env = environment
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    env.use_user(1)
    assert (
        await env.client.put("/api/v1/a2a/settings", json={"enabled": False})
    ).status_code == 403
    env.use_user(0)
    off = await env.client.put("/api/v1/a2a/settings", json={"enabled": False})
    assert off.status_code == 200 and not off.json()["enabled"]
    async with env.factory() as session:
        setting = await session.get(A2ASettings, 1)
        assert setting is not None and not setting.enabled
        job = await session.get(Job, UUID(task["id"]))
        assert job is not None and job.status == "pending"
    assert (await env.client.get("/.well-known/agent-card.json")).status_code == 503
    for method in ["SendMessage", "GetTask", "ListTasks", "CancelTask"]:
        response = await env.client.post(
            "/a2a", json={"jsonrpc": "2.0", "id": "off", "method": method, "params": message()}
        )
        assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert len(env.started) == 1
    assert (await env.client.get(f"/api/v1/jobs/{task['id']}")).status_code == 200
    assert (await env.client.put("/api/v1/a2a/settings", json={"enabled": True})).json()["enabled"]
    assert (await env.rpc("GetTask", {"id": task["id"]}))["result"]["id"] == task["id"]


async def test_lifecycle_cancellation_and_disconnect(environment: Environment) -> None:
    env = environment
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    canceled = (await env.rpc("CancelTask", {"id": task["id"]}))["result"]
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
    assert (await env.rpc("CancelTask", {"id": task["id"]}))["result"] == canceled
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    await env.state(task["id"], "awaiting_feedback")
    assert (await env.rpc("GetTask", {"id": task["id"]}))["result"]["status"][
        "state"
    ] == "TASK_STATE_INPUT_REQUIRED"
    await env.state(task["id"], "failed")
    assert (await env.rpc("GetTask", {"id": task["id"]}))["result"]["status"][
        "state"
    ] == "TASK_STATE_FAILED"
    assert (await env.rpc("CancelTask", {"id": task["id"]}))["error"]["code"] == -32002
    params = message("blocking request")
    params["configuration"] = {}
    pending = asyncio.create_task(env.rpc("SendMessage", params))
    async with asyncio.timeout(15):
        while len(env.started) < 3:
            await asyncio.sleep(0.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    running_id = env.started[-1]
    await env.state(running_id, "completed")
    assert (await env.rpc("GetTask", {"id": running_id}))["result"]["status"][
        "state"
    ] == "TASK_STATE_COMPLETED"


@pytest.mark.parametrize(
    ("method", "params", "code"),
    [
        ("missing", {}, -32601),
        ("SendStreamingMessage", {}, -32004),
        ("GetTask", {}, -32602),
        ("GetTask", {"id": "bad"}, -32001),
        ("GetTask", {"id": "bad", "historyLength": -1}, -32602),
        ("ListTasks", {"pageSize": -1}, -32602),
        ("ListTasks", {"pageToken": "-1"}, -32602),
        ("SendMessage", message(" "), -32602),
        ("SendMessage", message(contextId="other"), -32004),
        ("SendMessage", message(taskId="other"), -32004),
        ("SendMessage", message(role="ROLE_AGENT"), -32602),
        ("SendMessage", message(extensions=["https://example.org/extension"]), -32004),
        ("SendMessage", message(referenceTaskIds=["other"]), -32004),
        ("SendMessage", message(parts=[{"url": "file:///tmp/data"}]), -32005),
        ("SendMessage", message(parts=[{"text": "data", "mediaType": "text/html"}]), -32005),
    ],
)
async def test_validation(
    environment: Environment, method: str, params: dict[str, Any], code: int
) -> None:
    assert (await environment.rpc(method, params))["error"]["code"] == code
    assert not environment.started


async def test_bad_envelopes_and_settings(environment: Environment) -> None:
    env = environment
    assert (await env.client.post("/a2a", content="{")).json()["error"]["code"] == -32700
    for body in [
        [],
        {},
        {"jsonrpc": "2.0", "id": True, "method": "ListTasks"},
        {"jsonrpc": "1.0", "id": "bad", "method": "ListTasks"},
    ]:
        assert (await env.client.post("/a2a", json=body)).json()["error"]["code"] == -32600
    for settings_body in [{}, {"enabled": "false"}, {"enabled": 1}, {"enabled": False, "extra": 1}]:
        assert (await env.client.put("/api/v1/a2a/settings", json=settings_body)).status_code == 422
    env.client.headers["A2A-Version"] = "0.3"
    assert (await env.rpc("ListTasks"))["error"]["code"] == -32009


@pytest.mark.parametrize(
    ("configuration", "code"),
    [({"acceptedOutputModes": ["image/png"]}, -32005), ({"historyLength": -1}, -32602)],
)
async def test_unsupported_send_configuration(
    environment: Environment, configuration: dict[str, Any], code: int
) -> None:
    params = message()
    params["configuration"] = configuration
    assert (await environment.rpc("SendMessage", params))["error"]["code"] == code
    assert not environment.started


async def test_protocol_extensions_and_tenants_are_rejected(environment: Environment) -> None:
    env = environment
    assert (await env.rpc("ListTasks", {"tenant": "other"}))["error"]["code"] == -32602
    env.client.headers["A2A-Extensions"] = "https://example.org/extension"
    assert (await env.rpc("ListTasks"))["error"]["code"] == -32004


async def test_non_cancellable_manager_state(environment: Environment) -> None:
    env = environment
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    await env.state(task["id"], "generating_report")
    assert (await env.rpc("CancelTask", {"id": task["id"]}))["error"]["code"] == -32002
    assert (await env.rpc("GetTask", {"id": task["id"]}))["result"]["status"][
        "state"
    ] == "TASK_STATE_WORKING"


async def test_cancel_requires_persisted_confirmation(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    # A manager returning before it persists cancellation must not imply success.
    monkeypatch.setattr(env.manager, "cancel_job", lambda _job_id: None)
    assert (await env.rpc("CancelTask", {"id": task["id"]}))["error"]["code"] == -32002
    assert (await env.rpc("GetTask", {"id": task["id"]}))["result"]["status"][
        "state"
    ] == "TASK_STATE_SUBMITTED"


async def test_rls_cannot_read_other_users_protocol_metadata(environment: Environment) -> None:
    env = environment
    task = (await env.rpc("SendMessage", message()))["result"]["task"]
    async with env.app_session() as session:
        await set_current_user(session, env.users[1].id)
        assert await session.get(A2ATask, UUID(task["id"])) is None
        setting = await session.get(A2ASettings, 1)
        assert setting is not None
        setting.enabled = False
        with pytest.raises(Exception, match="row-level security|expected to update|permission"):
            await session.commit()

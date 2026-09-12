"""A2A wire, ownership and lifecycle tests against real PostgreSQL records."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from a2a.types import a2a_pb2 as wire
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openscientist.api import a2a, auth
from openscientist.api.endpoints import jobs
from openscientist.database.models import A2ASettings, A2ATask, Administrator, APIKey, Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.job_manager import JobManager
from openscientist.settings import clear_settings_cache


@dataclass
class Environment:
    client: AsyncClient
    factory: async_sessionmaker[AsyncSession]
    manager: JobManager
    users: list[User]
    tokens: list[str]
    started: list[str]
    app_session: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    admin_session: Callable[[], AbstractAsyncContextManager[AsyncSession]]

    def use_user(self, index: int) -> None:
        self.client.headers["Authorization"] = f"Bearer {self.tokens[index]}"

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.client.post(
            "/a2a", json={"jsonrpc": "2.0", "id": "test", "method": method, "params": params or {}}
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    async def state(self, task_id: str, state: str) -> None:
        async with self.factory() as session:
            job = await session.get(Job, UUID(task_id))
            assert job is not None
            job.status = state
            await session.commit()


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


@pytest_asyncio.fixture
async def environment(
    test_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Environment]:
    """Use the real manager and DB; replace only budget lookup and execution."""
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSCIENTIST_MODEL", "claude-test-model")
    monkeypatch.setenv("APP_URL", "https://example.org/scientist")
    clear_settings_cache()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def admin_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    @asynccontextmanager
    async def app_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            await session.execute(text("SET ROLE openscientist_app"))
            try:
                yield session
            finally:
                await session.rollback()
                await session.execute(text("RESET ROLE"))
                await session.commit()

    monkeypatch.setattr(a2a, "get_session_ctx", app_session)
    monkeypatch.setattr(a2a, "get_admin_session", admin_session)
    monkeypatch.setattr(auth, "get_admin_session", admin_session)
    users = [
        User(
            email=f"a2a-{uuid4()}@example.org",
            name="A2A Test",
            is_approved=i != 2,
            ntfy_enabled=False,
        )
        for i in range(3)
    ]
    tokens = []
    async with factory() as session:
        session.add_all(users)
        await session.flush()
        session.add(Administrator(user_id=users[0].id))
        for user in users:
            secret = uuid4().hex
            session.add(APIKey(user_id=user.id, name="client", key_hash=auth.hash_secret(secret)))
            tokens.append(f"client:{secret}")
        setting = await session.get(A2ASettings, 1)
        assert setting is not None
        setting.enabled = True
        await session.commit()

    manager = await asyncio.to_thread(JobManager, jobs_dir=tmp_path)
    started: list[str] = []
    monkeypatch.setattr(manager, "_check_budget_before_creation", lambda: None)
    monkeypatch.setattr(manager, "start_job", started.append)
    monkeypatch.setattr(manager, "_start_next_queued_job", lambda: None)
    monkeypatch.setattr(a2a, "_get_job_manager", lambda: manager)
    monkeypatch.setattr(jobs, "_get_job_manager", lambda: manager)
    app = FastAPI()

    async def rest_session() -> AsyncIterator[AsyncSession]:
        async with app_session() as session:
            yield session

    app.dependency_overrides[get_session] = rest_session
    app.include_router(a2a.router)
    app.include_router(jobs.router, prefix="/api/v1")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            env = Environment(
                client, factory, manager, users, tokens, started, app_session, admin_session
            )
            env.use_user(0)
            yield env
    finally:
        async with factory() as session:
            await session.execute(delete(Job).where(Job.owner_id.in_([u.id for u in users])))
            await session.execute(delete(User).where(User.id.in_([u.id for u in users])))
            setting = await session.get(A2ASettings, 1)
            assert setting is not None
            setting.enabled = True
            await session.commit()
        clear_settings_cache()


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
    for body in [[], {}, {"jsonrpc": "2.0", "id": True, "method": "ListTasks"}]:
        assert (await env.client.post("/a2a", json=body)).json()["error"]["code"] == -32600
    for settings_body in [{}, {"enabled": "false"}, {"enabled": 1}, {"enabled": False, "extra": 1}]:
        assert (await env.client.put("/api/v1/a2a/settings", json=settings_body)).status_code == 422
    env.client.headers["A2A-Version"] = "0.3"
    assert (await env.rpc("ListTasks"))["error"]["code"] == -32009


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

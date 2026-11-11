"""Tests for the web-side execution broker and its transport client."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import TestClient
from starlette.types import ASGIApp
from uvicorn.config import Config

from openscientist import exec_broker
from openscientist.exec_broker import _PLOT_LIMIT, _RESULT_FIELD_LIMIT, create_exec_broker_app
from openscientist.exec_broker_client import (
    EXEC_TOKEN_HEADER,
    BrokerError,
    execute_code_via_broker,
)
from openscientist.job_container.secrets import issue_run_credentials, revoke_run_credentials
from openscientist.settings import get_settings
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row

_HOST_PROJECT = "/host/proj"

_EXPECTED_KWARGS = {
    "code",
    "job_id",
    "data_path",
    "output_dir",
    "timeout",
    "description",
    "iteration",
    "data_files",
    "language",
}


@pytest.fixture
def broker_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the broker's confinement at a fixed /app <-> /host/proj mapping."""
    settings = SimpleNamespace(
        container=SimpleNamespace(
            host_project_dir=_HOST_PROJECT,
            container_app_dir="/app",
            executor_max_concurrent_per_job=2,
        )
    )
    monkeypatch.setattr(exec_broker, "get_settings", lambda: settings)


@pytest.fixture
def token_for() -> Iterator[Callable[[str], str]]:
    """Issue a live run's broker token for a job, ended after the test."""
    roles: list[str] = []

    def issue(job_id: str) -> str:
        role = f"agent_{uuid4().hex}"
        roles.append(role)
        return issue_run_credentials(job_id, role, "discovery").exec_token

    yield issue
    for role in roles:
        revoke_run_credentials(role)


def _client(manager: MagicMock) -> TestClient:
    return TestClient(create_exec_broker_app(manager=lambda: manager))


def _manager(result: dict[str, Any]) -> MagicMock:
    manager = MagicMock()
    manager.execute_code.return_value = result
    return manager


def _body(job_id: str = "job-1", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": "print(1)",
        "language": "python",
        "job_id": job_id,
        "output_dir": f"{_HOST_PROJECT}/jobs/{job_id}/provenance",
        "data_path": None,
        "data_files": [],
        "description": "",
        "iteration": 0,
        "timeout": 60,
    }
    body.update(overrides)
    return body


@pytest.mark.usefixtures("broker_settings")
class TestExecBrokerServer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancel_request", [False, True])
    async def test_concurrent_executions_are_limited_per_job(
        self, token_for: Callable[[str], str], cancel_request: bool
    ) -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Queue[str]()
        release = Event()
        manager = _manager({})

        def execute(*, job_id: str, **kwargs: object) -> dict[str, object]:
            loop.call_soon_threadsafe(entered.put_nowait, job_id)
            if job_id == "job-1":
                if not release.wait(timeout=15):
                    raise TimeoutError("job-1 executions were not released")
            return {"success": True}

        manager.execute_code.side_effect = execute
        tokens = {job: token_for(job) for job in ("job-1", "job-2")}
        app = create_exec_broker_app(manager=lambda: manager)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://broker"
        ) as client:

            async def post(job_id: str) -> httpx.Response:
                return await asyncio.wait_for(
                    client.post(
                        "/execute",
                        json=_body(job_id, timeout=None),
                        headers={EXEC_TOKEN_HEADER: tokens[job_id]},
                    ),
                    timeout=10,
                )

            running = [asyncio.create_task(post("job-1")) for _ in range(2)]
            try:
                for _ in running:
                    assert await asyncio.wait_for(entered.get(), timeout=5) == "job-1"
                if cancel_request:
                    running[0].cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await running[0]
                refused = await post("job-1")
                assert refused.status_code == 429, refused.text
                assert "concurrent execution limit" in refused.json()["error"]
                other = await post("job-2")
                assert other.status_code == 200
                assert await asyncio.wait_for(entered.get(), timeout=5) == "job-2"
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*running, return_exceptions=True), timeout=5)
            assert (await post("job-1")).status_code == 200

    def test_valid_request_runs_executor_and_returns_result(self, token_for) -> None:
        result = {"success": True, "output": "1", "plots": [], "execution_time": 0.1}
        manager = _manager(result)
        token = token_for("job-1")
        resp = _client(manager).post("/execute", json=_body(), headers={EXEC_TOKEN_HEADER: token})
        assert resp.status_code == 200
        assert resp.json() == result
        kwargs = manager.execute_code.call_args.kwargs
        assert kwargs["job_id"] == "job-1"
        # host path resolved back to the web container's own mount root
        assert kwargs["output_dir"] == Path("/app/jobs/job-1/provenance")

    def test_rejects_token_for_a_different_job(self, token_for) -> None:
        manager = _manager({})
        # A live token, but for job-2, presented with a body claiming job-1.
        token = token_for("job-2")
        resp = _client(manager).post(
            "/execute", json=_body(job_id="job-1"), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 401
        manager.execute_code.assert_not_called()

    def test_rejects_missing_token(self) -> None:
        manager = _manager({})
        resp = _client(manager).post("/execute", json=_body())
        assert resp.status_code == 401
        manager.execute_code.assert_not_called()

    def test_refuses_a_run_that_has_ended(self) -> None:
        manager = _manager({})
        role = f"agent_{uuid4().hex}"
        token = issue_run_credentials("job-1", role, "discovery").exec_token
        revoke_run_credentials(role)
        resp = _client(manager).post("/execute", json=_body(), headers={EXEC_TOKEN_HEADER: token})
        assert resp.status_code == 401
        manager.execute_code.assert_not_called()

    def test_ignores_non_execute_code_fields(self, token_for) -> None:
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.0})
        token = token_for("job-1")
        body = _body(
            network_mode="host",
            volumes={"/etc": {"bind": "/etc"}},
            cap_add=["NET_ADMIN"],
            entrypoint=["sh", "-c", "id"],
            user="root",
        )
        resp = _client(manager).post("/execute", json=body, headers={EXEC_TOKEN_HEADER: token})
        assert resp.status_code == 200
        kwargs = manager.execute_code.call_args.kwargs
        # Only execute_code-shaped params reach the spawner, never a container spec.
        assert set(kwargs) == _EXPECTED_KWARGS

    def test_rejects_output_dir_outside_job_directory(self, token_for) -> None:
        manager = _manager({})
        token = token_for("job-1")
        body = _body(output_dir=f"{_HOST_PROJECT}/jobs/other-job/provenance")
        resp = _client(manager).post("/execute", json=body, headers={EXEC_TOKEN_HEADER: token})
        assert resp.status_code == 403
        manager.execute_code.assert_not_called()

    def test_rejects_data_file_outside_job_directory(self, token_for) -> None:
        manager = _manager({})
        token = token_for("job-1")
        body = _body(data_files=[{"path": f"{_HOST_PROJECT}/jobs/victim/data/secret.csv"}])
        resp = _client(manager).post("/execute", json=body, headers={EXEC_TOKEN_HEADER: token})
        assert resp.status_code == 403
        manager.execute_code.assert_not_called()

    def test_rejects_unsupported_language(self, token_for) -> None:
        manager = _manager({})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=_body(language="ruby"), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 400
        manager.execute_code.assert_not_called()

    def test_translates_and_confines_data_paths(self, token_for) -> None:
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.0})
        token = token_for("job-1")
        body = _body(
            data_path=f"{_HOST_PROJECT}/jobs/job-1/data/primary.csv",
            data_files=[
                {"path": f"{_HOST_PROJECT}/jobs/job-1/data/primary.csv", "name": "primary.csv"}
            ],
        )
        resp = _client(manager).post("/execute", json=body, headers={EXEC_TOKEN_HEADER: token})
        assert resp.status_code == 200
        kwargs = manager.execute_code.call_args.kwargs
        assert kwargs["data_path"] == "/app/jobs/job-1/data/primary.csv"
        assert kwargs["data_files"][0]["path"] == "/app/jobs/job-1/data/primary.csv"
        assert kwargs["data_files"][0]["name"] == "primary.csv"

    @pytest.fixture(autouse=True)
    async def admin_settings_row(self, test_engine: AsyncEngine):
        """The admin row the route reads for the timeout cap, reset between tests."""
        await set_admin_settings_row(test_engine)
        try:
            yield
        finally:
            await reset_admin_settings_row(test_engine)


@pytest.mark.usefixtures("broker_settings")
class TestExecBrokerTimeout:
    """The /execute route caps the requested timeout at the row's executor timeout."""

    @pytest.fixture(autouse=True)
    async def admin_settings_row(self, test_engine: AsyncEngine):
        """The admin row the route reads for the cap, reset between tests."""
        await set_admin_settings_row(test_engine)
        try:
            yield
        finally:
            await reset_admin_settings_row(test_engine)

    async def test_a_timeout_far_above_the_row_reaches_the_executor_as_the_row(
        self, test_engine: AsyncEngine, token_for
    ) -> None:
        await set_admin_settings_row(test_engine, executor_timeout_seconds=120)
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=_body(timeout=10_000_000), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        assert manager.execute_code.call_args.kwargs["timeout"] == 120

    async def test_a_timeout_below_the_row_is_honoured(
        self, test_engine: AsyncEngine, token_for
    ) -> None:
        await set_admin_settings_row(test_engine, executor_timeout_seconds=300)
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=_body(timeout=45), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        assert manager.execute_code.call_args.kwargs["timeout"] == 45

    async def test_an_absent_timeout_leaves_the_executors_default(
        self, test_engine: AsyncEngine, token_for
    ) -> None:
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        body = _body()
        body.pop("timeout")
        resp = _client(manager).post("/execute", json=body, headers={EXEC_TOKEN_HEADER: token})
        assert resp.status_code == 200
        assert manager.execute_code.call_args.kwargs["timeout"] is None

    async def test_rust_a_timeout_above_the_row_is_held_at_the_rust_floor(
        self, test_engine: AsyncEngine, token_for
    ) -> None:
        await set_admin_settings_row(test_engine, executor_timeout_seconds=120)
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=_body(language="rust", timeout=300), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        assert manager.execute_code.call_args.kwargs["timeout"] == 300

    async def test_rust_a_timeout_far_above_the_floor_is_capped_at_the_rust_floor(
        self, test_engine: AsyncEngine, token_for
    ) -> None:
        await set_admin_settings_row(test_engine, executor_timeout_seconds=120)
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute",
            json=_body(language="rust", timeout=10_000_000),
            headers={EXEC_TOKEN_HEADER: token},
        )
        assert resp.status_code == 200
        assert manager.execute_code.call_args.kwargs["timeout"] == 300

    async def test_rust_a_row_above_the_floor_stays_the_cap(
        self, test_engine: AsyncEngine, token_for
    ) -> None:
        await set_admin_settings_row(test_engine, executor_timeout_seconds=400)
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute",
            json=_body(language="rust", timeout=10_000_000),
            headers={EXEC_TOKEN_HEADER: token},
        )
        assert resp.status_code == 200
        assert manager.execute_code.call_args.kwargs["timeout"] == 400

    async def test_a_fractional_timeout_floors_to_whole_seconds(
        self, test_engine: AsyncEngine, token_for
    ) -> None:
        manager = _manager({"success": True, "output": "", "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=_body(timeout=45.9), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        assert manager.execute_code.call_args.kwargs["timeout"] == 45

    @pytest.mark.parametrize("bad_timeout", [0, -5, "30", True, [60], {"seconds": 60}])
    async def test_invalid_timeouts_are_refused_before_the_executor(
        self, test_engine: AsyncEngine, token_for, bad_timeout
    ) -> None:
        manager = _manager({})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=_body(timeout=bad_timeout), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 422
        manager.execute_code.assert_not_called()

    @pytest.mark.parametrize("non_finite", ["1e400", "NaN", "-Infinity"])
    async def test_non_finite_timeouts_are_refused_before_the_executor(
        self, test_engine: AsyncEngine, token_for, non_finite
    ) -> None:
        """Wire literals that json.loads turns into non-finite floats."""
        manager = _manager({})
        token = token_for("job-1")
        body = _body()
        body["timeout"] = "@@T@@"
        wire = json.dumps(body).replace('"@@T@@"', non_finite)
        resp = _client(manager).post(
            "/execute",
            content=wire,
            headers={EXEC_TOKEN_HEADER: token, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        manager.execute_code.assert_not_called()


@pytest.mark.usefixtures("broker_settings")
class TestExecBrokerResultBounds:
    """The executor runs untrusted code, so the route caps the result's size."""

    def _no_timeout_body(self) -> dict[str, object]:
        body = _body()
        body.pop("timeout")
        return body

    def test_an_oversized_output_is_truncated(self, token_for) -> None:
        big = "x" * 100_000
        manager = _manager({"success": True, "output": big, "plots": [], "execution_time": 0.1})
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=self._no_timeout_body(), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        out = resp.json()["output"]
        assert len(out) <= _RESULT_FIELD_LIMIT
        assert out.startswith("x" * 1_000)
        assert len(out) < len(big)

    def test_an_oversized_error_is_truncated(self, token_for) -> None:
        manager = _manager(
            {
                "success": False,
                "output": "",
                "error": "e" * 100_000,
                "plots": [],
                "execution_time": 0.1,
            }
        )
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=self._no_timeout_body(), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        assert len(resp.json()["error"]) <= _RESULT_FIELD_LIMIT

    def test_an_oversized_traceback_is_truncated(self, token_for) -> None:
        manager = _manager(
            {
                "success": False,
                "output": "",
                "error": "boom",
                "traceback": "t" * 100_000,
                "plots": [],
                "execution_time": 0.1,
            }
        )
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=self._no_timeout_body(), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        assert len(resp.json()["traceback"]) <= _RESULT_FIELD_LIMIT

    def test_too_many_plots_are_bounded(self, token_for) -> None:
        manager = _manager(
            {
                "success": True,
                "output": "",
                "plots": [f"/output/plot_{i}.png" for i in range(500)],
                "execution_time": 0.1,
            }
        )
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=self._no_timeout_body(), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        plots = resp.json()["plots"]
        assert len(plots) == _PLOT_LIMIT
        assert plots[0] == "/output/plot_0.png"

    def test_a_small_result_passes_through_unchanged(self, token_for) -> None:
        result = {
            "success": True,
            "output": "42",
            "plots": ["/output/plot_1.png"],
            "execution_time": 0.1,
        }
        manager = _manager(result)
        token = token_for("job-1")
        resp = _client(manager).post(
            "/execute", json=self._no_timeout_body(), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 200
        assert resp.json() == result


class TestStartExecBroker:
    """The always-on broker listener wires the real broker app into its task."""

    async def test_start_wires_broker_app_and_starts_once(self, token_for, monkeypatch) -> None:
        configs: list[Config] = []
        served: list[socket.socket] = []

        class _FakeServer:
            def __init__(self, config: Config) -> None:
                configs.append(config)

            async def serve(self, sockets: list[socket.socket]) -> None:
                served.extend(sockets)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        monkeypatch.setenv("OPENSCIENTIST_EXEC_BROKER_PORT", str(port))
        get_settings.cache_clear()
        monkeypatch.setattr(exec_broker, "_NoSignalServer", _FakeServer)
        monkeypatch.setattr(exec_broker, "_broker_server", None)
        monkeypatch.setattr(exec_broker, "_broker_task", None)

        try:
            await exec_broker.start_exec_broker()
            await exec_broker.start_exec_broker()
            await asyncio.sleep(0)

            # One listener on the configured broker port, holding the broker app.
            assert len(configs) == 1
            assert exec_broker._broker_task is not None
            assert [sock.getsockname()[1] for sock in served] == [port]
        finally:
            for sock in served:
                sock.close()
            get_settings.cache_clear()

        # The wired-up app is the real broker: a live token of another job is refused.
        token = token_for("job-2")
        app = cast("ASGIApp", configs[0].app)
        resp = TestClient(app).post(
            "/execute", json=_body(job_id="job-1"), headers={EXEC_TOKEN_HEADER: token}
        )
        assert resp.status_code == 401


class TestExecBrokerClient:
    def test_posts_and_returns_parsed_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class _Resp:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, Any]:
                return {"success": True, "output": "ok", "plots": [], "execution_time": 0.2}

        def fake_post(url: str, *, json: Any, headers: dict[str, str], timeout: float) -> _Resp:
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return _Resp()

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("OPENSCIENTIST_EXEC_TOKEN", "job-1.abc")
        monkeypatch.setenv("OPENSCIENTIST_EXEC_BROKER_URL", "http://web:8082")

        result = execute_code_via_broker(
            code="print(1)",
            language="python",
            job_id="job-1",
            output_dir="/host/jobs/job-1/provenance",
            timeout=60,
        )
        assert result["success"] is True
        assert captured["url"] == "http://web:8082/execute"
        assert captured["headers"][EXEC_TOKEN_HEADER] == "job-1.abc"
        assert captured["json"]["job_id"] == "job-1"
        assert captured["timeout"] == 120.0

    def test_non_200_raises_broker_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Resp:
            status_code = 500
            text = "boom"

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
        monkeypatch.setenv("OPENSCIENTIST_EXEC_TOKEN", "t")
        with pytest.raises(BrokerError):
            execute_code_via_broker(
                code="x", language="python", job_id="j", output_dir="/o", timeout=1
            )

    def test_transport_error_raises_broker_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: Any, **k: Any) -> None:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", boom)
        with pytest.raises(BrokerError):
            execute_code_via_broker(
                code="x", language="python", job_id="j", output_dir="/o", timeout=1
            )

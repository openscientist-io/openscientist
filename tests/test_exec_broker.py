"""Tests for the web-side execution broker and its transport client."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.types import ASGIApp
from uvicorn.config import Config

from openscientist import exec_broker
from openscientist.exec_broker import create_exec_broker_app
from openscientist.exec_broker_client import (
    EXEC_TOKEN_HEADER,
    BrokerError,
    execute_code_via_broker,
)
from openscientist.job_container.secrets import issue_run_credentials, revoke_run_credentials
from openscientist.settings import get_settings

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
        container=SimpleNamespace(host_project_dir=_HOST_PROJECT, container_app_dir="/app")
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

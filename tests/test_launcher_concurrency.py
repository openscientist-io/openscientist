"""The launcher keeps answering while a long Docker call is in progress."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from openscientist_launcher import api
from openscientist_launcher.api import create_app
from tests.test_launcher_validation import JOB_ID, _config

_AGENT_DIGEST = "sha256:" + "a" * 64
_EXECUTOR_DIGEST = "sha256:" + "b" * 64


class _BlockingContainer:
    """An executor container whose wait holds until the test releases it."""

    id = "e" * 64
    short_id = "e" * 12
    name = "openscientist-exec-test"
    status = "running"

    def __init__(self) -> None:
        self.waiting = threading.Event()
        self.released = threading.Event()

    def wait(self, timeout: int | None = None) -> dict[str, int]:
        self.waiting.set()
        self.released.wait(timeout=3)
        return {"StatusCode": 0}

    def logs(self, **_kwargs: object) -> bytes:
        return b"done\n"

    def remove(self, **_kwargs: object) -> None:
        pass


class _FakeDocker:
    """The calls the exec, health and list routes make, answered in memory."""

    def __init__(self, container: _BlockingContainer) -> None:
        self.containers = SimpleNamespace(
            run=lambda **_kwargs: container,
            list=lambda **_kwargs: [],
        )
        digests = {"agent:latest": _AGENT_DIGEST, "executor:latest": _EXECUTOR_DIGEST}
        self.images = SimpleNamespace(
            get=lambda image: SimpleNamespace(attrs={"Id": digests[image]})
        )

    def ping(self) -> bool:
        return True


def _exec_body(jobs: Path) -> dict[str, object]:
    job_dir = jobs / JOB_ID
    (job_dir / "out").mkdir()
    (job_dir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return {
        "job_id": JOB_ID,
        "timeout_seconds": 60,
        "memory_mib": 1024,
        "cpu": 0.5,
        "egress_allow": ["openscientist:8081", "openscientist:8082"],
        "input": {
            "code": "print(1)",
            "language": "python",
            "data_path": None,
            "output_dir": str(job_dir / "out"),
            "timeout": 30,
            "description": "test",
            "iteration": 0,
            "data_files": [{"path": str(job_dir / "data.csv"), "name": "data.csv"}],
        },
    }


@pytest.mark.asyncio
async def test_health_and_list_answer_while_an_executor_run_is_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = tmp_path / "jobs"
    (jobs / JOB_ID).mkdir(parents=True)
    container = _BlockingContainer()
    monkeypatch.setattr(api.docker, "from_env", lambda **_kwargs: _FakeDocker(container))
    app = create_app(_config(jobs))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://launcher") as client:
        run = asyncio.create_task(client.post("/v1/exec", json=_exec_body(jobs)))
        try:
            assert await asyncio.to_thread(container.waiting.wait, 5)
            health = await client.get("/v1/health")
            listed = await client.get("/v1/containers")
            assert not run.done(), "the executor run finished before the other requests"
        finally:
            container.released.set()
        result = await run

    assert health.json() == {"docker": True, "images": {"agent": True, "executor": True}}
    assert listed.json() == []
    assert result.json() == {"stdout": "done\n", "exit_code": 0}

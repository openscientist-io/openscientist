"""Real end-to-end check for JobManager.shutdown() — no mocks.

Exercises the actual production code paths against a real Postgres
(testcontainer) and a real Docker container: a genuine DB row, a genuine
container carrying the same labels JobContainerRunner looks for, and a real
background thread shaped like _run_job_in_container's poll loop. Verifies
that after shutdown() runs, the DB row is cancelled (not stuck at RUNNING)
and the container has actually been removed.

This is heavier and slower than the mocked unit tests in test_job_manager.py
and is meant as a one-off confidence check, not a fast-running unit test.
"""

import threading
import time
from typing import Any, cast
from uuid import uuid4

import pytest

import docker
from openscientist.job_container import JobContainerRunner
from openscientist.job_manager import (
    JobManager,
    JobStatus,
    _db_create_job,
    _db_delete_job,
    _db_get_job,
)


async def test_shutdown_real_docker_and_postgres(tmp_path, test_engine):
    job_id = str(uuid4())

    # Real DB insert via job_manager's own production helper (a genuine
    # commit, visible across connections — unlike the db_session fixture's
    # rollback-wrapped transaction, which other connections can't see).
    await _db_create_job(job_id, research_question="E2E shutdown check", max_iterations=10)

    docker_client = docker.from_env()
    container_name = f"e2e-shutdown-check-{job_id[:8]}"
    docker_client.containers.run(
        "alpine:latest",
        command=["sleep", "300"],
        detach=True,
        labels={"openscientist.job_id": job_id, "openscientist.type": "agent"},
        name=container_name,
    )

    def remaining_containers() -> list[Any]:
        return cast(
            "list[Any]",
            docker_client.containers.list(
                all=True, filters={"label": f"openscientist.job_id={job_id}"}
            ),
        )

    try:
        manager = JobManager(jobs_dir=tmp_path, max_concurrent=1)
        # In production, _run_job_in_container flips this to RUNNING itself
        # as the first thing it does after the thread starts.
        manager._update_job_status(job_id, JobStatus.RUNNING)

        def fake_run_job_in_container() -> None:
            """Mirrors _run_job_in_container's poll loop and cleanup shape."""
            runner = JobContainerRunner()
            try:
                terminal = {"completed", "failed", "cancelled"}
                for _ in range(150):  # 15s bound, comfortably > shutdown timeout below
                    info = manager.get_job(job_id)
                    if info and info.status.value in terminal:
                        return
                    time.sleep(0.1)
            finally:
                runner.cleanup(job_id)
                with manager._lock:
                    manager._running_jobs.pop(job_id, None)

        thread = threading.Thread(target=fake_run_job_in_container, daemon=True)
        manager._running_jobs[job_id] = thread
        thread.start()

        manager.shutdown(timeout=10.0)

        assert not thread.is_alive(), "worker thread did not exit after shutdown()"

        job_model = await _db_get_job(job_id)
        assert job_model is not None, "job row disappeared"
        assert job_model.status == JobStatus.CANCELLED.value, (
            f"job left in status={job_model.status!r} instead of cancelled"
        )
        assert job_model.cancellation_reason == "Server shut down while job was running"

        assert remaining_containers() == [], "container was not cleaned up by shutdown"
    finally:
        # Best-effort cleanup in case an assertion failed before the thread's
        # own cleanup ran. The DB row in particular must not leak: this test
        # commits for real (unlike the db_session fixture's rollback-wrapped
        # transaction), and an orphaned (owner_id=None) job row bypasses RLS
        # ownership filtering, so a leftover row here corrupts job-count and
        # RLS assertions in every test that runs after this one.
        for c in remaining_containers():
            c.remove(force=True)
        await _db_delete_job(job_id)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

"""An end-to-end job through the launcher.

The web's agent path (launch, exit polling, cleanup, the reaper's label
listing and the chat turn) runs against the real launcher process and Docker
daemon, the way the other launcher integration tests do. The launcher is the
only process that holds the Docker socket in this loop: every container the
web's path created carries the launcher's marker label, and the web's code
holds no Docker client of its own (the posture tests pin that).
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import docker
from openscientist.database.models import Job as JobModel
from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_manager import JobStatus
from openscientist.settings import clear_settings_cache
from tests.admin_row import set_admin_settings_row
from tests.launcher import LAUNCHER_INSTANCE_ID, Launcher, image_digest

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_AGENT_IMAGE = "openscientist-agent-fake:latest"
MARKER_LABEL = "openscientist.launcher"


async def _wait_for(
    what: str, check: Callable[[], Awaitable[bool]], deadline_seconds: float
) -> None:
    """Poll until the check passes; fail naming what the deadline covered."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.5)
    pytest.fail(f"{what} did not happen within {deadline_seconds:.0f}s")


@pytest.mark.integration
class TestAgentPathThroughLauncher:
    """A job runs to its terminal status while the launcher alone talks to Docker."""

    @pytest.fixture(autouse=True)
    def check_docker(self):
        client = docker.from_env()
        try:
            client.ping()
            try:
                client.images.get(FAKE_AGENT_IMAGE)
            except docker.errors.ImageNotFound:
                subprocess.run(
                    [
                        "docker",
                        "build",
                        "-f",
                        str(REPO_ROOT / "docker" / "Dockerfile.agent-fake"),
                        "-t",
                        FAKE_AGENT_IMAGE,
                        str(REPO_ROOT),
                    ],
                    check=True,
                    capture_output=True,
                )
        except docker.errors.DockerException:
            client.close()
            pytest.skip("Docker is required")
        client.close()

    @pytest.fixture
    def jobs_root(self, tmp_path: Path) -> Path:
        jobs = tmp_path / "jobs"
        jobs.mkdir()
        return jobs

    @pytest.fixture
    def launcher(self, tmp_path: Path, jobs_root: Path) -> Generator[Launcher, None, None]:
        client = docker.from_env()
        digest = image_digest(client, FAKE_AGENT_IMAGE)
        started = Launcher.start(
            tmp_path,
            jobs_root,
            AGENT_IMAGE=FAKE_AGENT_IMAGE,
            EXECUTOR_IMAGE=FAKE_AGENT_IMAGE,
            AGENT_IMAGE_DIGESTS=digest,
            EXECUTOR_IMAGE_DIGESTS=digest,
            # The admin row's default job lifetime is four hours.
            MAX_LIFETIME_SECONDS="14400",
        )
        try:
            yield started
        finally:
            started.close()

    @pytest.fixture
    def runner(
        self, launcher: Launcher, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[JobContainerRunner, None, None]:
        # The anthropic provider keeps RUN_HOME unset, so no harness home is
        # provisioned and the run's labels stay limited to the login. The key is
        # never used: the fake agent exits before any model call.
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-e2e")
        monkeypatch.setenv("OPENSCIENTIST_LAUNCHER_SOCKET_PATH", str(launcher.socket_path))
        monkeypatch.setenv("OPENSCIENTIST_HOST_PROJECT_DIR", "")
        clear_settings_cache()
        try:
            yield JobContainerRunner()
        finally:
            clear_settings_cache()

    @pytest_asyncio.fixture
    async def job(self, test_engine: AsyncEngine, jobs_root: Path) -> AsyncGenerator[str, None]:
        await set_admin_settings_row(test_engine)
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with factory() as session:
            model = JobModel(
                research_question="An end-to-end question?", status=JobStatus.PENDING.value
            )
            session.add(model)
            await session.commit()
            job_id = str(model.id)
        # The web creates the job directory when the job is created; the
        # launcher validates the id against it.
        (jobs_root / job_id).mkdir(parents=True)
        try:
            yield job_id
        finally:
            async with factory() as session:
                row = (
                    await session.execute(select(JobModel).where(JobModel.id == job_id))
                ).scalar_one()
                await session.delete(row)
                await session.commit()

    async def _status(self, test_engine: AsyncEngine, job_id: str) -> str:
        async with test_engine.connect() as conn:
            return str(
                (
                    await conn.execute(select(JobModel.status).where(JobModel.id == job_id))
                ).scalar_one()
            )

    async def _error(self, test_engine: AsyncEngine, job_id: str) -> str | None:
        async with test_engine.connect() as conn:
            value = (
                await conn.execute(select(JobModel.error_message).where(JobModel.id == job_id))
            ).scalar_one()
            return None if value is None else str(value)

    @pytest.mark.asyncio
    async def test_a_job_reaches_its_terminal_status_through_the_launcher(
        self,
        test_engine: AsyncEngine,
        launcher: Launcher,
        runner: JobContainerRunner,
        jobs_root: Path,
        job: str,
    ) -> None:
        """Launch, exit polling, cleanup and the reaper's listing all reach Docker
        through the launcher, and the job ends failed on the container's exit code."""
        from openscientist.job_manager import JobManager

        manager = JobManager(jobs_dir=jobs_root)
        docker_client = docker.from_env()
        try:
            await asyncio.to_thread(manager.start_job, job)

            async def live() -> bool:
                listed = await asyncio.to_thread(runner.live_job_ids)
                return listed is not None and job in listed

            await _wait_for("the reaper's listing to carry the live job", live, 60)

            async def marked_only() -> bool:
                listed = await asyncio.to_thread(
                    lambda: docker_client.containers.list(
                        all=True, filters={"label": f"openscientist.job_id={job}"}
                    )
                )
                return bool(listed) and all(
                    container.labels.get(MARKER_LABEL) == LAUNCHER_INSTANCE_ID
                    for container in listed
                )

            await _wait_for("the launcher-created agent container to appear", marked_only, 30)

            async def terminal() -> bool:
                return (await self._status(test_engine, job)) != JobStatus.RUNNING.value

            await _wait_for("the job to reach a terminal status", terminal, 180)

            assert await self._status(test_engine, job) == JobStatus.FAILED.value
            assert "exited with code 1" in (await self._error(test_engine, job) or "")

            async def cleaned() -> bool:
                listed = await asyncio.to_thread(
                    lambda: docker_client.containers.list(
                        all=True, filters={"label": f"openscientist.job_id={job}"}
                    )
                )
                gone_from_runner = await asyncio.to_thread(
                    lambda: runner._find_container(job) is None
                )
                return not listed and gone_from_runner

            await _wait_for("the launcher to remove the container", cleaned, 30)
        finally:
            docker_client.close()

    @pytest.mark.asyncio
    async def test_a_chat_turn_runs_through_the_launcher(
        self,
        runner: JobContainerRunner,
        jobs_root: Path,
        job: str,
    ) -> None:
        """The chat turn waits on and removes its container through the launcher,
        and a non-zero exit surfaces the container's logs."""
        docker_client = docker.from_env()
        try:
            turn = asyncio.ensure_future(
                asyncio.to_thread(runner.run_chat_turn, job, jobs_root / job, "A chat turn")
            )

            async def marked_chat() -> bool:
                listed = await asyncio.to_thread(
                    lambda: docker_client.containers.list(
                        all=True,
                        filters={
                            "label": [
                                f"openscientist.job_id={job}",
                                "openscientist.type=chat",
                            ]
                        },
                    )
                )
                return bool(listed) and all(
                    container.labels.get(MARKER_LABEL) == LAUNCHER_INSTANCE_ID
                    for container in listed
                )

            await _wait_for("the launcher-created chat container to appear", marked_chat, 30)
            with pytest.raises(RuntimeError, match="exited with code 1"):
                await turn

            async def removed() -> bool:
                listed = await asyncio.to_thread(
                    lambda: docker_client.containers.list(
                        all=True,
                        filters={
                            "label": [
                                f"openscientist.job_id={job}",
                                "openscientist.type=chat",
                            ]
                        },
                    )
                )
                return not listed

            await _wait_for("the chat container to be removed", removed, 30)
        finally:
            docker_client.close()

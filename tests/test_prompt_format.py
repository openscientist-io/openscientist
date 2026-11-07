"""The prompt-formatting step: a bounded turn, before the iteration-1 budget
check, that records the research question as the display fragment. Failure is
never fatal, so the job continues to discovery with the plain question."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from openscientist.agent.base import IterationResult, TurnOutcome
from openscientist.database.models import Job, User
from openscientist.orchestrator import discovery
from openscientist.orchestrator.discovery import run_discovery_async
from openscientist.orchestrator.prompt_format import format_research_prompt

QUESTION = (
    "Why do cold-adapted cells accumulate carnosine? "
    "See the `execute_code` notebook for the raw counts."
)
IMAGE = "/jobs/figure.png"
CSV = "/jobs/counts.csv"


@asynccontextmanager
async def committed(engine: AsyncEngine):
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


def _completed() -> IterationResult:
    return IterationResult(
        outcome=TurnOutcome.COMPLETED, output="done", tool_calls=1, transcript=[]
    )


def _executor(turn: object) -> SimpleNamespace:
    """A fake agent that records nothing but its turns' prompts."""
    return SimpleNamespace(
        prompt_fragments=lambda: SimpleNamespace(mcp_tool_prefix=""),
        shutdown=AsyncMock(),
        apply_runtime_environment=lambda: None,
        prepare_job_workspace=AsyncMock(),
        warm_model_profile=AsyncMock(),
        run_iteration=AsyncMock(side_effect=turn),
    )


async def _column(run: SimpleNamespace) -> str | None:
    async with committed(run.engine) as session:
        value = await session.scalar(select(Job.formatted_prompt).where(Job.id == UUID(run.job_id)))
    return cast("str | None", value)


async def _set_column(run: SimpleNamespace, value: str | None) -> None:
    async with run.engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET formatted_prompt = :value WHERE id = :job"),
            {"value": value, "job": run.job_id},
        )


@pytest_asyncio.fixture
async def fmt_job(test_engine: AsyncEngine, tmp_path: Path):
    """A running job on a real database, with its job directory."""
    async with committed(test_engine) as session:
        owner = User(email=f"owner-{uuid4().hex[:12]}@example.com", name="Owner")
        session.add(owner)
        await session.flush()
        job = Job(
            owner_id=owner.id,
            research_question=QUESTION,
            status="running",
            max_iterations=1,
            llm_provider="anthropic",
            llm_config={"model": "claude-sonnet-4-6"},
        )
        session.add(job)
        await session.commit()
        job_id = str(job.id)

    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield SimpleNamespace(job_id=job_id, job_dir=job_dir, engine=test_engine)
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE id = :job"), {"job": job_id})
            await conn.execute(text("DELETE FROM users WHERE id = :user"), {"user": owner.id})


class TestFormatResearchPrompt:
    """The step itself, against a real job row and a fake executor."""

    @pytest.mark.asyncio
    async def test_skips_the_turn_when_already_set(self, fmt_job) -> None:
        """A resumed job already holds its fragment, so no turn runs."""
        await _set_column(fmt_job, "<p>already recorded</p>")
        executor = _executor(AssertionError("the turn must not run"))
        await format_research_prompt(executor, fmt_job.job_id, QUESTION, [IMAGE, CSV])  # type: ignore[arg-type]
        executor.run_iteration.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_records_on_the_first_turn_and_stops(self, fmt_job) -> None:
        async def turn(prompt: str, *, reset_session: bool = False) -> IterationResult:
            assert reset_session is True
            assert "set_formatted_prompt" in prompt
            assert QUESTION in prompt
            assert "data/figure.png" in prompt
            assert "data/counts.csv" in prompt
            await _set_column(fmt_job, "<p>recorded</p>")
            return _completed()

        executor = _executor(turn)
        await format_research_prompt(executor, fmt_job.job_id, QUESTION, [IMAGE, CSV])  # type: ignore[arg-type]
        executor.run_iteration.assert_awaited_once()
        assert await _column(fmt_job) == "<p>recorded</p>"

    @pytest.mark.asyncio
    async def test_retries_once_when_the_first_turn_records_nothing(self, fmt_job) -> None:
        executor = _executor(lambda prompt, **kwargs: _completed())
        await format_research_prompt(executor, fmt_job.job_id, QUESTION, [IMAGE, CSV])  # type: ignore[arg-type]
        assert executor.run_iteration.await_count == 2
        first, second = [call.args[0] for call in executor.run_iteration.await_args_list]
        assert "did NOT record" in second
        assert "did NOT record" not in first
        assert executor.run_iteration.await_args.kwargs["reset_session"] is False
        assert await _column(fmt_job) is None

    @pytest.mark.asyncio
    async def test_a_failed_turn_ends_the_step(self, fmt_job) -> None:
        executor = _executor(
            IterationResult(
                outcome=TurnOutcome.FAILED,
                output="",
                tool_calls=0,
                transcript=[],
                error="the turn exploded",
            )
        )
        await format_research_prompt(executor, fmt_job.job_id, QUESTION, [IMAGE, CSV])  # type: ignore[arg-type]
        assert executor.run_iteration.await_count == 1
        assert await _column(fmt_job) is None

    @pytest.mark.asyncio
    async def test_a_timed_out_turn_ends_the_step(self, fmt_job) -> None:
        executor = _executor(
            IterationResult(
                outcome=TurnOutcome.TIMED_OUT, output="partial", tool_calls=0, transcript=[]
            )
        )
        await format_research_prompt(executor, fmt_job.job_id, QUESTION, [IMAGE, CSV])  # type: ignore[arg-type]
        assert executor.run_iteration.await_count == 1

    @pytest.mark.asyncio
    async def test_a_raising_turn_ends_the_step(self, fmt_job) -> None:
        async def turn(prompt: str, *, reset_session: bool = False) -> IterationResult:
            raise RuntimeError("the model blew up")

        executor = _executor(turn)
        await format_research_prompt(executor, fmt_job.job_id, QUESTION, [IMAGE, CSV])  # type: ignore[arg-type]
        assert executor.run_iteration.await_count == 1

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, fmt_job) -> None:
        async def turn(prompt: str, *, reset_session: bool = False) -> IterationResult:
            raise asyncio.CancelledError()

        executor = _executor(turn)
        with pytest.raises(asyncio.CancelledError):
            await format_research_prompt(executor, fmt_job.job_id, QUESTION, [IMAGE, CSV])  # type: ignore[arg-type]


class TestFormatTurnInDiscoveryLoop:
    """The step's place in the loop, on a real job row."""

    @pytest.mark.asyncio
    async def test_the_format_turn_runs_before_the_iteration_one_budget_check(
        self, fmt_job, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sequence: list[str] = []
        prompts: list[str] = []

        async def turn(prompt: str, *, reset_session: bool = False) -> IterationResult:
            sequence.append("turn")
            prompts.append(prompt)
            if await _column(fmt_job) is None:
                await _set_column(fmt_job, "<p>beautified</p>")
            return _completed()

        async def ask_budget(**kwargs) -> SimpleNamespace:
            sequence.append("budget-check")
            return SimpleNamespace(ok=True, message=None)

        with (
            patch(
                "openscientist.orchestrator.discovery._build_agent_executor",
                return_value=_executor(turn),
            ),
            patch(
                "openscientist.orchestrator.discovery._sync_version_metadata",
                new_callable=AsyncMock,
            ),
            patch("openscientist.orchestrator.discovery._append_iteration_artifacts"),
            patch(
                "openscientist.orchestrator.discovery._run_report_generation_phase",
                new_callable=AsyncMock,
            ),
            patch(
                "openscientist.orchestrator.discovery._persist_final_status",
                new_callable=AsyncMock,
                return_value="completed",
            ),
            monkeypatch.context() as ctx,
        ):
            ctx.setattr(discovery, "ask_budget", ask_budget)
            result = await run_discovery_async(fmt_job.job_dir)

        assert sequence == ["turn", "budget-check", "turn"]
        format_prompt, iteration_prompt = prompts
        assert "set_formatted_prompt" in format_prompt
        assert QUESTION in format_prompt
        assert "set_formatted_prompt" not in iteration_prompt
        assert QUESTION in iteration_prompt
        assert await _column(fmt_job) == "<p>beautified</p>"
        assert result["status"] == "completed"

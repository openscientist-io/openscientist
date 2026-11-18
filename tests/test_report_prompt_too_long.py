"""A report turn that overflows the model's context retries in a fresh session."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.agent.base import IterationResult, TurnOutcome
from openscientist.database.models import CostRecord, Job
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator import report
from tests.test_budget import committed

_WINDOW = 1_000_000


@pytest_asyncio.fixture
async def job_dir(test_engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[Path]:
    """A job whose last billed call held 255,000 tokens of context, with a later
    estimated row that carries no measured context."""
    async with committed(test_engine) as session:
        job = Job(research_question="Q?")
        session.add(job)
        await session.commit()
        for operation, input_tokens, cache_read in (
            ("discovery", 8_000, 180_000),
            ("report", 5_000, 250_000),
            ("report_estimated", 900_000, 0),
        ):
            session.add(
                CostRecord(
                    job_id=job.id,
                    operation_type=operation,
                    provider="foundry",
                    model="claude-opus-5",
                    input_tokens=input_tokens,
                    output_tokens=1_000,
                    cache_read_tokens=cache_read,
                    cost_usd=1.0,
                )
            )
            await session.commit()
        directory = tmp_path / str(job.id)
        directory.mkdir()
        try:
            yield directory
        finally:
            await session.execute(delete(CostRecord).where(CostRecord.job_id == job.id))
            await session.execute(delete(Job).where(Job.id == job.id))
            await session.commit()


def _executor(job_dir: Path, *, writes_on: set[int]) -> SimpleNamespace:
    calls: list[bool] = []

    async def run(prompt: str, *, reset_session: bool = False) -> IterationResult:
        calls.append(reset_session)
        if len(calls) in writes_on:
            (job_dir / "final_report.md").write_text("# Report\n", encoding="utf-8")
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="Written.", tool_calls=1, transcript=[]
            )
        return IterationResult(
            outcome=TurnOutcome.COMPLETED, output="Prompt is too long", tool_calls=0, transcript=[]
        )

    return SimpleNamespace(
        run_iteration=run,
        file_write_tool="Write",
        model_profile=SimpleNamespace(context_window_tokens=_WINDOW),
        calls=calls,
    )


def _budgets(prompt_builder) -> list[int]:
    return [call.kwargs["context_window_tokens"] for call in prompt_builder.call_args_list]


async def test_an_overflowing_report_turn_retries_in_a_fresh_session_with_a_trimmed_prompt(
    job_dir: Path, caplog
):
    caplog.set_level(logging.WARNING, logger="openscientist.orchestrator.report")
    executor = _executor(job_dir, writes_on={2})
    with patch.object(report, "build_report_prompt", return_value="REPORT") as prompt:
        _, written = await report._run_report_turn(
            executor,  # type: ignore[arg-type]
            job_dir,
            "Q?",
            KnowledgeState(job_dir.name, "Q?", 3),
            None,
        )
    assert written is True
    assert executor.calls == [False, True]
    assert _budgets(prompt) == [_WINDOW, 255_000 // 2]
    (warning,) = [
        record.message
        for record in caplog.records
        if record.name == report.logger.name and record.levelno == logging.WARNING
    ]
    assert "too long" in warning
    assert "255000 tokens" in warning


async def test_a_report_that_keeps_overflowing_fails_naming_the_overflow(job_dir: Path, caplog):
    caplog.set_level(logging.ERROR, logger="openscientist.orchestrator.report")
    executor = _executor(job_dir, writes_on=set())
    with patch.object(report, "build_report_prompt", return_value="REPORT") as prompt:
        result, written = await report._run_report_turn(
            executor,  # type: ignore[arg-type]
            job_dir,
            "Q?",
            KnowledgeState(job_dir.name, "Q?", 3),
            None,
        )
    assert written is False
    assert executor.calls == [False, True, True]
    assert _budgets(prompt) == [_WINDOW, 255_000 // 2, 255_000 // 4]
    assert "too long for the model's context" in result.error
    assert "255000 tokens" in result.error
    assert not any("did not write" in record.message for record in caplog.records)


async def test_an_overflow_without_a_known_context_halves_the_window(tmp_path: Path):
    executor = _executor(tmp_path, writes_on={2})
    with patch.object(report, "build_report_prompt", return_value="REPORT") as prompt:
        _, written = await report._run_report_turn(
            executor,  # type: ignore[arg-type]
            tmp_path,
            "Q?",
            KnowledgeState("j", "Q?", 3),
            None,
        )
    assert written is True
    assert executor.calls == [False, True]
    assert _budgets(prompt) == [_WINDOW, _WINDOW // 2]


@pytest.mark.parametrize("field", ["output", "error"])
def test_the_overflow_is_recognised_in_the_output_or_the_error(field):
    message = "API Error: 400 prompt is too long: 1012000 tokens > 1000000 maximum"
    result = IterationResult(
        outcome=TurnOutcome.FAILED if field == "error" else TurnOutcome.COMPLETED,
        output=message if field == "output" else "",
        tool_calls=0,
        transcript=[],
        error=message if field == "error" else "",
    )
    assert report._prompt_too_long(result)
    assert not report._prompt_too_long(
        IterationResult(
            outcome=TurnOutcome.COMPLETED, output="I wrote it.", tool_calls=1, transcript=[]
        )
    )

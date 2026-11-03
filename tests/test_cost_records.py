"""A run's cost is written as it goes, so a stop or a failed write loses little
(plans/cost-record-loss.md)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.agent.base import (
    AbstractAgent,
    AgentConfig,
    IterationResult,
    TokenUsage,
    TurnOutcome,
)
from openscientist.database.models import CostRecord, Job
from openscientist.job_chat import _chat_turn_files, run_chat_turn_async
from openscientist.job_container.db_login import AGENT_LOGIN_LABEL
from openscientist.job_container.launcher_client import LauncherClient, LauncherError
from openscientist.job_container.runner import _STOP_GRACE_SECONDS, JobContainerRunner
from openscientist.orchestrator.discovery import (
    regenerate_report_async,
    run_discovery_async,
)
from openscientist.orchestrator.provenance import _CostLedger
from openscientist.orchestrator.report import _ReportOutcome
from openscientist.providers.base import Provider
from tests.db_outage import Forwarder
from tests.helpers import StubClaudeProvider


@pytest_asyncio.fixture
async def job(test_engine: AsyncEngine) -> AsyncGenerator[UUID, None]:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        row = Job(research_question="Q?", status="running")
        session.add(row)
        await session.commit()
    yield row.id
    async with AsyncSession(test_engine) as session:
        await session.execute(delete(Job).where(Job.id == row.id))
        await session.commit()


async def _records(test_engine: AsyncEngine, job: UUID) -> list[tuple[str, int | None, int, int]]:
    async with test_engine.connect() as conn:
        rows = await conn.execute(
            select(
                CostRecord.operation_type,
                CostRecord.iteration,
                CostRecord.input_tokens,
                CostRecord.output_tokens,
            )
            .where(CostRecord.job_id == job)
            .order_by(CostRecord.created_at)
        )
    return [tuple(row) for row in rows]


def _ledger(job: UUID, usage: TokenUsage) -> _CostLedger:
    return _CostLedger(
        str(job), usage=lambda: usage, provider_name="Anthropic", model_name="claude-sonnet-4-5"
    )


async def test_each_record_holds_only_its_own_tokens(test_engine: AsyncEngine, job: UUID) -> None:
    usage = TokenUsage()  # grows in place, as an agent's running total does
    ledger = _ledger(job, usage)
    ledger.begin("discovery", 1)
    usage += TokenUsage(input_tokens=100, output_tokens=10)
    await ledger.record()
    ledger.begin("discovery", 2)
    usage += TokenUsage(input_tokens=40, output_tokens=4)
    await ledger.record()
    ledger.begin("report", None)
    await ledger.close()
    assert await _records(test_engine, job) == [("discovery", 1, 100, 10), ("discovery", 2, 40, 4)]


async def test_a_failed_write_carries_its_tokens_to_the_next(
    database: Forwarder, test_engine: AsyncEngine, job: UUID
) -> None:
    usage = TokenUsage()
    ledger = _ledger(job, usage)
    ledger.begin("discovery", 1)
    usage += TokenUsage(input_tokens=100, output_tokens=10)
    database.down()
    await ledger.record()
    database.up()
    assert await _records(test_engine, job) == []
    ledger.begin("report", None)
    usage += TokenUsage(input_tokens=40, output_tokens=4)
    await ledger.record()
    assert await _records(test_engine, job) == [("report", None, 140, 14)]


async def test_a_failed_last_write_logs_every_count(
    database: Forwarder, job: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    usage = TokenUsage()
    ledger = _ledger(job, usage)
    ledger.begin("discovery", 3)
    usage += TokenUsage(
        input_tokens=11,
        output_tokens=22,
        cache_write_tokens=33,
        cache_write_1h_tokens=44,
        cache_read_tokens=55,
        reasoning_tokens=66,
    )
    database.down()
    with caplog.at_level(logging.ERROR):
        await ledger.close()
    [message] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    for count in ("input=11", "output=22", "cache_write=33", "cache_write_1h=44"):
        assert count in message
    for count in ("cache_read=55", "reasoning=66", "claude-sonnet-4-5", str(job)):
        assert count in message


def _executor(usage: TokenUsage) -> MagicMock:
    """A stand-in executor whose running total is ``usage`` and whose provider
    and model are plain strings the cost record can hold."""
    executor = MagicMock()
    executor.total_tokens = usage
    executor.shutdown = AsyncMock()
    executor.prepare_job_workspace = AsyncMock()
    executor.warm_model_profile = AsyncMock()
    executor.provider.display_name = "anthropic"
    executor.provider.effective_model_name.return_value = "claude-sonnet-4-5"
    executor.config.model_override = None
    return executor


async def test_a_completed_run_writes_each_phase_cost_as_it_goes(
    database: Forwarder, test_engine: AsyncEngine, job: UUID, tmp_path: Path
) -> None:
    """The second iteration and the report write their own records while the
    run goes on, so the billing table shows every phase of a finished run."""
    usage = TokenUsage()
    iteration_tokens = [(100, 10), (40, 4)]

    async def run_iteration(_prompt: str, *, reset_session: bool) -> IterationResult:
        input_tokens, output_tokens = iteration_tokens.pop(0)
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        return IterationResult(
            outcome=TurnOutcome.COMPLETED, output="done", tool_calls=0, transcript=[]
        )

    async def report_phase(
        executor, job_dir, research_question, description=None
    ) -> _ReportOutcome:
        usage.input_tokens += 20
        usage.output_tokens += 2
        return _ReportOutcome(success=True, error="")

    mock_executor = _executor(usage)
    mock_executor.run_iteration = run_iteration
    runtime = {
        "job_id": str(job),
        "research_question": "Q?",
        "max_iterations": 2,
        "use_hypotheses": False,
        "investigation_mode": "autonomous",
        "data_files": [],
    }
    job_dir = tmp_path / str(job)
    job_dir.mkdir(parents=True)

    with (
        patch(
            "openscientist.orchestrator.discovery._load_runtime_context",
            new_callable=AsyncMock,
            return_value=runtime,
        ),
        patch(
            "openscientist.orchestrator.discovery._build_agent_executor",
            return_value=mock_executor,
        ),
        patch(
            "openscientist.orchestrator.discovery._run_report_generation_phase",
            side_effect=report_phase,
        ),
        patch(
            "openscientist.orchestrator.discovery._persist_final_status",
            new_callable=AsyncMock,
            return_value="completed",
        ),
        patch("openscientist.orchestrator.discovery.update_job_status", new_callable=AsyncMock),
        patch("openscientist.orchestrator.discovery._append_iteration_artifacts"),
        patch("openscientist.orchestrator.discovery._sync_version_metadata"),
        patch(
            "openscientist.orchestrator.discovery._wait_for_coinvestigate_feedback",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("openscientist.orchestrator.discovery.increment_ks_iteration"),
        patch(
            "openscientist.orchestrator.discovery._get_job_status",
            new_callable=AsyncMock,
            return_value="running",
            create=True,
        ),
    ):
        result = await run_discovery_async(job_dir)

    assert result["status"] == "completed"
    assert await _records(test_engine, job) == [
        ("discovery", 1, 100, 10),
        ("discovery", 2, 40, 4),
        ("report", None, 20, 2),
    ]
    mock_executor.shutdown.assert_awaited_once()


async def test_report_regeneration_writes_only_the_report_cost(
    database: Forwarder, test_engine: AsyncEngine, job: UUID, tmp_path: Path
) -> None:
    """Regenerating the report of a finished job bills the report turn as a
    report, and not as a discovery iteration."""
    usage = TokenUsage()

    async def report_phase(
        executor, job_dir, research_question, description=None
    ) -> _ReportOutcome:
        usage.input_tokens += 30
        usage.output_tokens += 3
        return _ReportOutcome(success=True, error="")

    mock_executor = _executor(usage)
    runtime: dict[str, object] = {
        "job_id": str(job),
        "research_question": "Q?",
        "description": None,
        "max_iterations": 1,
        "use_hypotheses": False,
        "investigation_mode": "autonomous",
        "data_files": [],
    }
    job_dir = tmp_path / str(job)
    job_dir.mkdir(parents=True)

    with (
        patch(
            "openscientist.orchestrator.discovery._load_runtime_context",
            new_callable=AsyncMock,
            return_value=runtime,
        ),
        patch(
            "openscientist.orchestrator.discovery._build_agent_executor",
            return_value=mock_executor,
        ),
        patch(
            "openscientist.orchestrator.discovery._run_report_generation_phase",
            side_effect=report_phase,
        ),
        patch(
            "openscientist.orchestrator.discovery._persist_final_status",
            new_callable=AsyncMock,
            return_value="completed",
        ),
        patch("openscientist.orchestrator.discovery.update_job_status", new_callable=AsyncMock),
    ):
        result = await regenerate_report_async(job_dir)

    assert result["status"] == "completed"
    assert await _records(test_engine, job) == [("report", None, 30, 3)]
    mock_executor.shutdown.assert_awaited_once()


def test_cleanup_takes_the_container_down_even_when_stop_fails(tmp_path: Path) -> None:
    """A stop error must not strand the container: the agent got its grace to
    write the in-flight cost, and teardown still saves the log, removes the
    container and drops its database login."""
    ref = "c" * 64
    entry = {
        "id": ref,
        "labels": {
            "openscientist.job_id": "job-123",
            "openscientist.type": "agent",
            AGENT_LOGIN_LABEL: "agent_test",
        },
    }
    client = MagicMock(spec=LauncherClient)
    client.list_containers.return_value = [entry]
    client.stop.side_effect = LauncherError("docker", "stop failed")
    client.logs.return_value = "cost written"
    with (
        patch("openscientist.job_container.runner.LauncherClient", return_value=client),
        patch("openscientist.job_container.runner.close_agent_login") as closed,
    ):
        JobContainerRunner().cleanup("job-123", tmp_path)
    client.stop.assert_called_once_with(ref, timeout_seconds=_STOP_GRACE_SECONDS)
    client.remove.assert_called_once_with(ref)
    closed.assert_called_once_with("agent_test")
    assert (tmp_path / "agent-container.log").read_text() == "cost written"


def _chat_request(job_dir: Path, *, model_override: str | None) -> None:
    _chat_turn_files(job_dir, "t1")[0].write_text(
        json.dumps({"system_prompt": "SP", "model_override": model_override, "prompt": "hi"})
    )


def _agent_spending(usage: TokenUsage, then: BaseException | None = None):
    """A `factory.build_agent` whose turn spends ``usage``, then raises ``then``."""
    from openscientist.agent import factory

    real_build_agent = factory.build_agent

    def build(config: AgentConfig, provider: Provider) -> AbstractAgent[Provider]:
        agent = real_build_agent(config, provider)

        async def run_iteration(prompt: str, **kwargs: object) -> IterationResult:
            agent._token_usage += usage
            if then is not None:
                raise then
            return IterationResult(
                outcome=TurnOutcome.COMPLETED, output="reply", tool_calls=0, transcript=[]
            )

        agent.run_iteration = run_iteration  # type: ignore[method-assign]
        return agent

    return build


async def _models(test_engine: AsyncEngine, job: UUID) -> list[str]:
    async with test_engine.connect() as conn:
        rows = await conn.execute(select(CostRecord.model).where(CostRecord.job_id == job))
    return [row[0] for row in rows]


async def test_a_chat_turn_records_its_cost_at_its_own_model(
    test_engine: AsyncEngine, job: UUID, tmp_path: Path
) -> None:
    _chat_request(tmp_path, model_override="claude-haiku-4-5")
    spent = TokenUsage(input_tokens=120, output_tokens=12)
    with (
        patch("openscientist.providers.get_provider", return_value=StubClaudeProvider()),
        patch("openscientist.agent.factory.build_agent", _agent_spending(spent)),
    ):
        status = await run_chat_turn_async(tmp_path, "t1", str(job))

    assert status["status"] == "completed"
    assert await _records(test_engine, job) == [("chat", None, 120, 12)]
    assert await _models(test_engine, job) == ["claude-haiku-4-5"]


async def test_a_stopped_chat_turn_still_records_what_it_spent(
    test_engine: AsyncEngine, job: UUID, tmp_path: Path
) -> None:
    """docker stop cancels the turn, and the cost it already ran up is kept."""
    _chat_request(tmp_path, model_override=None)
    spent = TokenUsage(input_tokens=70, output_tokens=7)
    with (
        patch("openscientist.providers.get_provider", return_value=StubClaudeProvider()),
        patch(
            "openscientist.agent.factory.build_agent",
            _agent_spending(spent, then=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_chat_turn_async(tmp_path, "t1", str(job))

    assert await _records(test_engine, job) == [("chat", None, 70, 7)]

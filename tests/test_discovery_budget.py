"""The discovery loop's budget gate: the container asks the web before every
iteration, and a reached limit, or a check the web could not answer in time,
stops the iterations but not the report."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openscientist import admin_settings as admin_settings_mod
from openscientist.agent.base import AbstractAgent, IterationResult, TurnOutcome
from openscientist.database.models import CostRecord, Job, User
from openscientist.database.session import get_admin_session
from openscientist.exec_broker import create_exec_broker_app
from openscientist.exec_broker_client import ask_budget
from openscientist.job_container.secrets import issue_run_credentials, revoke_run_credentials
from openscientist.orchestrator import discovery
from openscientist.orchestrator.discovery import run_discovery_async
from openscientist.orchestrator.report import _begin_report_phase, _ReportOutcome
from openscientist.providers.base import CostInfo, Provider
from openscientist.settings import clear_settings_cache
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.db_logins import ADMIN_PASSWORD, login_url

MODEL = "claude-sonnet-4-6"
_LOGIN_PASSWORD = "budget-test-login"


@pytest_asyncio.fixture(autouse=True)
async def admin_settings_row(test_engine: AsyncEngine):
    """The limits the check reads, reset to the defaults between tests."""
    await set_admin_settings_row(test_engine)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


@pytest_asyncio.fixture(autouse=True)
def fresh_billing_cache():
    """The per-process billing cache starts empty, as a fresh web process would."""
    from openscientist import budget as budget_mod

    budget_mod._billed = None
    budget_mod._billed_at = 0.0
    yield
    budget_mod._billed = None
    budget_mod._billed_at = 0.0


@asynccontextmanager
async def committed(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


class _Run:
    """A running job and the environment of its container, on a real database."""

    def __init__(self, job_id: str, job_dir: Path, engine: AsyncEngine) -> None:
        self.job_id = job_id
        self.job_dir = job_dir
        self.engine = engine


@pytest_asyncio.fixture
async def run(
    test_engine: AsyncEngine,
    test_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Run]:
    """A job already running, a container login for it, and the environment a
    container would have, so the loop's database writes run through RLS."""
    async with committed(test_engine) as session:
        owner = User(email=f"owner-{uuid4().hex[:12]}@example.com", name="Owner")
        session.add(owner)
        await session.flush()
        job = Job(
            owner_id=owner.id,
            research_question="Q?",
            status="running",
            max_iterations=2,
            llm_provider="anthropic",
            llm_config={"model": MODEL},
        )
        session.add(job)
        await session.commit()
        job_id = str(job.id)
        owner_id = owner.id

    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    role = f"agent_{uuid4().hex}"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{_LOGIN_PASSWORD}' IN ROLE openscientist_agent"
            )
        )
        await conn.execute(
            text("INSERT INTO agent_job_roles (role_name, job_id) VALUES (:role, :job)"),
            {"role": role, "job": job_id},
        )

    monkeypatch.setenv("DATABASE_URL", login_url(test_database_url, role, _LOGIN_PASSWORD))
    monkeypatch.setenv(
        "ADMIN_DATABASE_URL", login_url(test_database_url, "openscientist_admin", ADMIN_PASSWORD)
    )
    monkeypatch.setenv(
        "OPENSCIENTIST_EXEC_TOKEN", issue_run_credentials(job_id, role, "discovery").exec_token
    )
    monkeypatch.setenv("OPENSCIENTIST_MODEL", MODEL)
    clear_settings_cache()
    try:
        yield _Run(job_id, job_dir, test_engine)
    finally:
        revoke_run_credentials(role)
        monkeypatch.undo()
        clear_settings_cache()
        async with test_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM agent_job_roles WHERE role_name = :role"), {"role": role}
            )
            await conn.execute(text(f"DROP ROLE {role}"))
            await conn.execute(
                text("DELETE FROM cost_records WHERE job_id = :job"), {"job": job_id}
            )
            await conn.execute(text("DELETE FROM jobs WHERE id = :job"), {"job": job_id})
            await conn.execute(text("DELETE FROM users WHERE id = :user"), {"user": owner_id})


@pytest.fixture
def ask_via_broker(run: _Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop's ask reaches the real broker app over an ASGI transport."""
    app = create_exec_broker_app(manager=lambda: MagicMock())
    monkeypatch.setattr(
        discovery, "ask_budget", partial(ask_budget, transport=httpx.ASGITransport(app=app))
    )


@pytest.fixture
def admin_settings_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings row read through the admin login, the way the web process can."""

    @asynccontextmanager
    async def _ctx():
        async with get_admin_session() as session:
            yield session

    monkeypatch.setattr(admin_settings_mod, "get_session_ctx", _ctx)


def _executor(run: _Run, growing: bool = False) -> MagicMock:
    """A fake agent whose turns complete at once; with ``growing`` each turn
    writes the cost row the LLM proxy would have, so the next check sees it."""
    executor = MagicMock()
    executor.shutdown = AsyncMock()
    executor.prepare_job_workspace = AsyncMock()
    executor.warm_model_profile = AsyncMock()
    executor.prompt_fragments = lambda: SimpleNamespace(mcp_tool_prefix="")
    executor.provider = SimpleNamespace(
        display_name="Anthropic", effective_model_name=lambda: MODEL
    )
    executor.config = SimpleNamespace(model_override=None)
    executor.model_profile = SimpleNamespace(context_window_tokens=131_072)

    async def run_iteration(prompt: str, reset_session: bool = False) -> IterationResult:
        if growing:
            await _proxy_row(run, input_tokens=100_000, output_tokens=50_000, cost=1.0)
        return IterationResult(
            outcome=TurnOutcome.COMPLETED, output="done", tool_calls=1, transcript=[]
        )

    executor.run_iteration = AsyncMock(side_effect=run_iteration)
    return executor


async def _proxy_row(run: _Run, *, input_tokens: int, output_tokens: int, cost: float) -> None:
    """The cost row the LLM proxy writes for a forwarded call, on the test engine."""
    async with committed(run.engine) as session:
        session.add(
            CostRecord(
                job_id=UUID(run.job_id),
                operation_type="discovery",
                provider="anthropic",
                model=MODEL,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            )
        )
        await session.commit()


def _report_phase() -> AsyncMock:
    """The report phase: the job moves to generating_report, then succeeds."""

    async def phase(
        executor: AbstractAgent[Provider],
        job_dir: Path,
        research_question: str,
        description: str | None,
    ) -> _ReportOutcome:
        await _begin_report_phase(job_dir)
        return _ReportOutcome(success=True, error="")

    return AsyncMock(side_effect=phase)


async def _run_discovery(run: _Run, executor: MagicMock) -> dict[str, str]:
    """run_discovery_async with the agent and the report phase faked."""
    with (
        patch("openscientist.orchestrator.discovery._build_agent_executor", return_value=executor),
        patch(
            "openscientist.orchestrator.discovery._sync_version_metadata", new_callable=AsyncMock
        ),
        patch("openscientist.orchestrator.discovery._append_iteration_artifacts"),
        patch("openscientist.orchestrator.discovery._run_report_generation_phase", _report_phase()),
    ):
        return await run_discovery_async(run.job_dir)


async def _row(run: _Run) -> dict[str, str | None]:
    async with committed(run.engine) as session:
        job = (await session.execute(select(Job).where(Job.id == UUID(run.job_id)))).scalar_one()
    return {"status": job.status, "stop_note": job.stop_note}


async def _set_status(run: _Run, status: str) -> None:
    async with run.engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET status = :status WHERE id = :job"),
            {"status": status, "job": run.job_id},
        )


async def _set_column(run: _Run, column: str, value: str) -> None:
    async with run.engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE jobs SET {column} = :value WHERE id = :job"),
            {"value": value, "job": run.job_id},
        )


async def _wait_for_status(run: _Run, status: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with committed(run.engine) as session:
            current = await session.scalar(select(Job.status).where(Job.id == UUID(run.job_id)))
        if current == status:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"the job never reached {status}")


def _billing_zero() -> SimpleNamespace:
    """The provider billing boundary, reporting no billed spend."""
    info = CostInfo(
        provider_name="stub",
        total_spend_usd=0.0,
        recent_spend_usd=0.0,
        recent_period_hours=24,
    )
    return SimpleNamespace(id="anthropic", get_cost_info=lambda lookback_hours=24: info)


async def _cost_row(run: _Run, cost: float) -> None:
    async with committed(run.engine) as session:
        session.add(
            CostRecord(
                job_id=UUID(run.job_id),
                operation_type="discovery",
                provider="anthropic",
                model=MODEL,
                input_tokens=100,
                output_tokens=100,
                cost_usd=cost,
            )
        )
        await session.commit()


class TestDiscoveryBudgetGate:
    """Every situation the loop can be in, against every answer the check gets."""

    async def test_allowed_iterations_run_and_the_job_completes_without_a_note(
        self, run: _Run, ask_via_broker: None, admin_settings_reader: None
    ) -> None:
        executor = _executor(run)
        result = await _run_discovery(run, executor)
        assert result["status"] == "completed"
        assert executor.run_iteration.await_count == 2  # both iterations
        row = await _row(run)
        assert row["status"] == "completed"
        assert row["stop_note"] is None

    async def test_a_job_cap_stops_the_first_iteration_and_the_report_still_runs(
        self, run: _Run, ask_via_broker: None, admin_settings_reader: None
    ) -> None:
        await _cost_row(run, 10.0)
        await set_admin_settings_row(run.engine, max_job_cost_usd=5.0)
        executor = _executor(run)
        result = await _run_discovery(run, executor)
        assert result["status"] == "completed"
        assert executor.run_iteration.await_count == 0  # the check runs before any iteration
        row = await _row(run)
        assert row["status"] == "completed"
        assert row["stop_note"] is not None
        assert "Job cost limit" in row["stop_note"]

    async def test_a_project_total_stops_a_later_iteration(
        self, run: _Run, ask_via_broker: None, admin_settings_reader: None
    ) -> None:
        """Iteration 1's ledger row spends past the limit, so the iteration-2
        check stops the run."""
        with patch("openscientist.budget.get_provider", return_value=_billing_zero()):
            await set_admin_settings_row(run.engine, max_project_spend_total_usd=0.5)
            executor = _executor(run, growing=True)
            result = await _run_discovery(run, executor)
        assert result["status"] == "completed"
        assert executor.run_iteration.await_count == 1
        row = await _row(run)
        assert row["status"] == "completed"
        assert row["stop_note"] is not None
        assert "Project spend total limit" in row["stop_note"]

    async def test_a_project_24h_stops_a_later_iteration(
        self, run: _Run, ask_via_broker: None, admin_settings_reader: None
    ) -> None:
        with patch("openscientist.budget.get_provider", return_value=_billing_zero()):
            await set_admin_settings_row(run.engine, max_project_spend_24h_usd=0.5)
            executor = _executor(run, growing=True)
            result = await _run_discovery(run, executor)
        assert result["status"] == "completed"
        assert executor.run_iteration.await_count == 1
        row = await _row(run)
        assert row["stop_note"] is not None
        assert "Project 24-hour spend limit" in row["stop_note"]

    async def test_a_coinvestigate_resume_flows_into_the_next_iteration_check(
        self, run: _Run, ask_via_broker: None, admin_settings_reader: None
    ) -> None:
        # The job pauses between iterations for the user's feedback.
        await _set_column(run, "investigation_mode", "coinvestigate")
        with patch("openscientist.budget.get_provider", return_value=_billing_zero()):
            executor = _executor(run, growing=True)
            task = asyncio.create_task(_run_discovery(run, executor))
            # The loop pauses after iteration 1, its cost row already written.
            await _wait_for_status(run, "awaiting_feedback")
            await set_admin_settings_row(run.engine, max_project_spend_total_usd=0.5)
            # The user resumes the job, as the feedback panel would.
            await _set_status(run, "running")
            result = await asyncio.wait_for(task, timeout=60.0)
        assert result["status"] == "completed"
        assert executor.run_iteration.await_count == 1  # iteration 1
        row = await _row(run)
        assert row["status"] == "completed"
        assert row["stop_note"] is not None
        assert "Project spend total limit" in row["stop_note"]

    async def test_the_check_window_exhausts_and_the_job_stops_with_the_note(
        self,
        run: _Run,
        admin_settings_reader: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The check can never answer: the settings row is missing, so the
        # broker answers errors until the window is spent.
        async with run.engine.begin() as conn:
            await conn.execute(text("DELETE FROM admin_settings"))
        app = create_exec_broker_app(manager=lambda: MagicMock())
        monkeypatch.setattr(
            discovery,
            "ask_budget",
            partial(ask_budget, window=2.0, transport=httpx.ASGITransport(app=app)),
        )
        executor = _executor(run)
        result = await _run_discovery(run, executor)
        assert result["status"] == "completed"
        assert executor.run_iteration.await_count == 0  # no iteration ran
        row = await _row(run)
        assert row["status"] == "completed"
        assert row["stop_note"] is not None
        assert "could not be checked" in row["stop_note"]

    async def test_an_authentication_refusal_stops_at_once(
        self,
        run: _Run,
        ask_via_broker: None,
        admin_settings_reader: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A token the run no longer holds: the broker refuses it at once.
        monkeypatch.setenv("OPENSCIENTIST_EXEC_TOKEN", f"{run.job_id}.deadbeef")
        executor = _executor(run)
        started = time.monotonic()
        result = await _run_discovery(run, executor)
        assert result["status"] == "completed"
        assert executor.run_iteration.await_count == 0  # no iteration ran
        assert time.monotonic() - started < 10.0  # not after the two-minute window
        row = await _row(run)
        assert row["status"] == "completed"
        assert row["stop_note"] is not None
        assert "could not be checked" in row["stop_note"]

    async def test_cancellation_wins_over_a_reached_limit(
        self,
        run: _Run,
        ask_via_broker: None,
        admin_settings_reader: None,
    ) -> None:
        # The limit is reached, but the job is cancelled while the loop is in
        # it: the existing cancellation check wins, so no report and no note.
        await _cost_row(run, 10.0)
        await set_admin_settings_row(run.engine, max_job_cost_usd=5.0)

        async def phase(
            executor: AbstractAgent[Provider],
            job_dir: Path,
            research_question: str,
            description: str | None,
        ) -> _ReportOutcome:
            await _set_status(run, "cancelled")
            await _begin_report_phase(job_dir)
            return _ReportOutcome(success=True, error="")

        executor = _executor(run)
        with (
            patch(
                "openscientist.orchestrator.discovery._build_agent_executor", return_value=executor
            ),
            patch(
                "openscientist.orchestrator.discovery._sync_version_metadata",
                new_callable=AsyncMock,
            ),
            patch("openscientist.orchestrator.discovery._append_iteration_artifacts"),
            patch(
                "openscientist.orchestrator.discovery._run_report_generation_phase",
                new_callable=AsyncMock,
                side_effect=phase,
            ),
        ):
            result = await run_discovery_async(run.job_dir)
        assert result["status"] == "cancelled"
        assert executor.run_iteration.await_count == 0  # no iteration ran
        row = await _row(run)
        assert row["status"] == "cancelled"
        assert row["stop_note"] is None

"""The broker's /budget route: the container's budget check against the ledger
and the provider's billed total, behind the run's own token."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

from openscientist import budget as budget_mod
from openscientist.database.models import Job
from openscientist.exec_broker import create_exec_broker_app
from openscientist.exec_broker_client import EXEC_TOKEN_HEADER
from openscientist.job_container.secrets import issue_run_credentials, revoke_run_credentials
from openscientist.providers.base import CostInfo
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.test_budget import job_with_costs

MODEL = "claude-sonnet-4-6"


@pytest_asyncio.fixture(autouse=True)
async def admin_settings_row(test_engine: AsyncEngine):
    """The limits the check reads, reset to the defaults between tests."""
    await set_admin_settings_row(test_engine)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


@pytest.fixture(autouse=True)
def fresh_billing_cache():
    """The per-process billing cache starts empty, as a fresh web process would."""
    budget_mod._billed = None
    budget_mod._billed_at = 0.0
    yield
    budget_mod._billed = None
    budget_mod._billed_at = 0.0


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


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_exec_broker_app(manager=lambda: MagicMock()))


@asynccontextmanager
async def committed(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


def _billing(total: float, recent: float) -> SimpleNamespace:
    info = CostInfo(
        provider_name="stub",
        total_spend_usd=total,
        recent_spend_usd=recent,
        recent_period_hours=24,
    )
    return SimpleNamespace(id="anthropic", get_cost_info=lambda lookback_hours=24: info)


class TestBudgetRoute:
    """The route answers check_budget for the job the run's token maps to."""

    async def test_it_answers_a_job_cap_against_the_real_ledger(
        self, test_engine: AsyncEngine, token_for, client
    ) -> None:
        async with job_with_costs(test_engine, [(10.0, None)]) as job:
            job_id = str(job.id)
            await set_admin_settings_row(test_engine, max_job_cost_usd=5.0)
            resp = client.post(
                "/budget",
                json={"job_id": job_id, "request_id": "req-1"},
                headers={EXEC_TOKEN_HEADER: token_for(job_id)},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["request_id"] == "req-1"
            assert body["ok"] is False
            assert body["limit"] == "job_cap"
            assert body["message"]

    async def test_it_allows_below_the_limits(
        self, test_engine: AsyncEngine, token_for, client
    ) -> None:
        async with job_with_costs(test_engine, [(0.5, None)]) as job:
            job_id = str(job.id)
            await set_admin_settings_row(test_engine, max_job_cost_usd=5.0)
            resp = client.post(
                "/budget",
                json={"job_id": job_id, "request_id": "req-2"},
                headers={EXEC_TOKEN_HEADER: token_for(job_id)},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["request_id"] == "req-2"
            assert body["ok"] is True
            assert body["limit"] is None
            assert body["message"] is None

    async def test_it_answers_the_project_total(
        self, test_engine: AsyncEngine, token_for, client
    ) -> None:
        async with job_with_costs(test_engine, [(10.0, None)]) as job:
            job_id = str(job.id)
            await set_admin_settings_row(test_engine, max_project_spend_total_usd=1.0)
            with patch("openscientist.budget.get_provider", return_value=_billing(0.0, 0.0)):
                resp = client.post(
                    "/budget",
                    json={"job_id": job_id, "request_id": "req-3"},
                    headers={EXEC_TOKEN_HEADER: token_for(job_id)},
                )
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is False
            assert body["limit"] == "project_total"

    async def test_it_answers_the_project_24h(
        self, test_engine: AsyncEngine, token_for, client
    ) -> None:
        async with job_with_costs(test_engine, [(10.0, None)]) as job:
            job_id = str(job.id)
            await set_admin_settings_row(test_engine, max_project_spend_24h_usd=1.0)
            with patch("openscientist.budget.get_provider", return_value=_billing(0.0, 0.0)):
                resp = client.post(
                    "/budget",
                    json={"job_id": job_id, "request_id": "req-4"},
                    headers={EXEC_TOKEN_HEADER: token_for(job_id)},
                )
            assert resp.status_code == 200
            assert resp.json()["limit"] == "project_24h"

    async def test_it_resolves_the_model_when_the_job_stores_none(
        self, test_engine: AsyncEngine, token_for, client
    ) -> None:
        from openscientist.database.models import CostRecord

        async with committed(test_engine) as session:
            job = Job(research_question="Q?", llm_provider="anthropic", llm_config=None)
            session.add(job)
            await session.commit()
            await session.refresh(job)
            job_id = str(job.id)
        try:
            async with committed(test_engine) as session:
                session.add(
                    CostRecord(
                        job_id=job.id,
                        operation_type="discovery",
                        provider="anthropic",
                        model=MODEL,
                        input_tokens=100,
                        output_tokens=100,
                        cost_usd=10.0,
                    )
                )
                await session.commit()
            await set_admin_settings_row(test_engine, max_job_cost_usd=5.0)
            with patch(
                "openscientist.job_manager.get_provider",
                return_value=SimpleNamespace(effective_model_name=lambda: MODEL),
            ):
                resp = client.post(
                    "/budget",
                    json={"job_id": job_id, "request_id": "req-5"},
                    headers={EXEC_TOKEN_HEADER: token_for(job_id)},
                )
            assert resp.status_code == 200
            assert resp.json()["limit"] == "job_cap"
        finally:
            async with committed(test_engine) as session:
                await session.execute(delete(CostRecord).where(CostRecord.job_id == job.id))
                await session.execute(delete(Job).where(Job.id == job.id))
                await session.commit()

    async def test_it_refuses_a_token_for_another_job(
        self, test_engine: AsyncEngine, token_for, client
    ) -> None:
        async with job_with_costs(test_engine, [(10.0, None)]) as job:
            job_id = str(job.id)
        try:
            token = token_for(f"other-{uuid4().hex}")
            resp = client.post(
                "/budget",
                json={"job_id": job_id, "request_id": "req-6"},
                headers={EXEC_TOKEN_HEADER: token},
            )
            assert resp.status_code == 401
        finally:
            async with committed(test_engine) as session:
                await session.execute(delete(Job).where(Job.id == UUID(job_id)))
                await session.commit()

    async def test_it_refuses_a_missing_token(self, test_engine: AsyncEngine, client) -> None:
        async with job_with_costs(test_engine, [(10.0, None)]) as job:
            resp = client.post("/budget", json={"job_id": str(job.id), "request_id": "req-7"})
            assert resp.status_code == 401

    async def test_it_refuses_a_missing_request_id(
        self, test_engine: AsyncEngine, token_for, client
    ) -> None:
        async with job_with_costs(test_engine, [(10.0, None)]) as job:
            job_id = str(job.id)
        try:
            resp = client.post(
                "/budget",
                json={"job_id": job_id},
                headers={EXEC_TOKEN_HEADER: token_for(job_id)},
            )
            assert resp.status_code == 400
        finally:
            async with committed(test_engine) as session:
                await session.execute(delete(Job).where(Job.id == UUID(job_id)))
                await session.commit()

    async def test_it_404s_when_the_job_is_gone(self, token_for, client) -> None:
        job_id = str(uuid4())
        token = token_for(job_id)
        resp = client.post(
            "/budget",
            json={"job_id": job_id, "request_id": "req-8"},
            headers={EXEC_TOKEN_HEADER: token},
        )
        assert resp.status_code == 404

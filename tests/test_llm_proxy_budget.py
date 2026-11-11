"""Budget enforcement at the authenticated proxy boundary."""

import asyncio
import json

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from openscientist.llm_proxy import create_llm_proxy_app
from openscientist.providers.base import LlmUpstream, StaticHeadersSigning
from tests import test_llm_proxy as proxy_fixtures
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.test_budget import _cost, committed, job_with_costs
from tests.test_llm_proxy import TestChatgptResponsesWebsocket as WebsocketFixtures
from tests.test_llm_proxy import _recording_upstream

placeholder_for = proxy_fixtures.placeholder_for


@pytest.fixture(autouse=True)
async def reset_limits(test_engine, monkeypatch):
    await set_admin_settings_row(test_engine)
    monkeypatch.setattr("openscientist.budget._provider_billing", _no_billing)
    try:
        yield
    finally:
        await reset_admin_settings_row(test_engine)


async def _no_billing():
    return None


@pytest.mark.parametrize(
    "limit", ["max_job_cost_usd", "max_project_spend_total_usd", "max_project_spend_24h_usd"]
)
@pytest.mark.parametrize("spent", [0.5, 1.0])
async def test_http_budget_gate(limit, spent, test_engine, placeholder_for):
    await set_admin_settings_row(test_engine, **{limit: 1.0})
    async with job_with_costs(test_engine, [(spent, None)]) as job:
        recorded = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://upstream.test", StaticHeadersSigning({"x-api-key": "REAL"})
            ),
            client=_recording_upstream(recorded),
            provider_id=lambda: "anthropic",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy"
        ) as client:
            response = await client.post(
                "/v1/messages",
                headers={"x-api-key": placeholder_for(str(job.id))},
                json={"model": "claude-sonnet-4-6", "messages": []},
            )
        assert response.status_code == (403 if spent == 1.0 else 200)
        assert len(recorded) == (0 if spent == 1.0 else 1)
        if spent == 1.0:
            assert "limit" in response.text


class TestWebsocketBudget:
    stub_upstream = WebsocketFixtures.stub_upstream
    live_proxy = WebsocketFixtures.live_proxy

    async def test_exhausted_handshake_never_reaches_upstream(
        self, test_engine, placeholder_for, live_proxy, stub_upstream
    ):
        await set_admin_settings_row(test_engine, max_job_cost_usd=1.0)
        async with job_with_costs(test_engine, [(1.0, None)]) as job:
            with pytest.raises(InvalidStatus) as raised:
                async with asyncio.timeout(5):
                    await connect(
                        f"ws://127.0.0.1:{live_proxy}/responses",
                        additional_headers={
                            "authorization": f"Bearer {placeholder_for(str(job.id))}"
                        },
                        proxy=None,
                    )
            assert raised.value.response.status_code == 403
            assert stub_upstream.handshakes == []

    @pytest.mark.parametrize("binary", [False, True])
    @pytest.mark.parametrize("model", ["gpt-5.2", None])
    async def test_open_connection_rechecks_each_model_call(
        self, binary, model, test_engine, placeholder_for, live_proxy, stub_upstream
    ):
        await set_admin_settings_row(test_engine, max_job_cost_usd=1.0)
        async with job_with_costs(test_engine, []) as job:
            async with (
                asyncio.timeout(5),
                connect(
                    f"ws://127.0.0.1:{live_proxy}/responses",
                    additional_headers={"authorization": f"Bearer {placeholder_for(str(job.id))}"},
                    proxy=None,
                ) as connection,
            ):
                frame = {"type": "response.create"}
                if model:
                    frame["model"] = model
                text = json.dumps(frame)
                create = text.encode() if binary else text
                await connection.send(create)
                assert await connection.recv() == "stub-reply"
                async with committed(test_engine) as session:
                    await _cost(session, job.id, 1.0)
                await connection.send(create)
                error = json.loads(await connection.recv())
                assert error["type"] == "error"
                assert error["status"] == 403
                assert "Job cost limit" in error["error"]["message"]
            assert stub_upstream.frames == [create]

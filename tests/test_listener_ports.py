"""The LLM proxy and execution broker listen on configurable ports and refuse
to start on a taken one (plans/listener-ports.md)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from openscientist import exec_broker, llm_proxy
from openscientist.exec_broker_client import container_broker_base_url
from openscientist.listener import ListenerBindError
from openscientist.providers.base import AirgapEgress
from openscientist.settings import clear_settings_cache


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def held_port() -> Iterator[int]:
    """A port another process already listens on."""
    with socket.socket() as holder:
        holder.bind(("0.0.0.0", 0))
        holder.listen()
        yield int(holder.getsockname()[1])


@pytest.fixture
def port_setting(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str, int], None]]:
    def set_port(name: str, port: int) -> None:
        monkeypatch.setenv(name, str(port))
        clear_settings_cache()

    yield set_port
    clear_settings_cache()


@pytest.fixture
def proxied_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SimpleNamespace(
        airgap_egress=lambda: SimpleNamespace(mode=AirgapEgress.PROXY),
        llm_upstream=lambda: None,
    )
    monkeypatch.setattr(llm_proxy, "get_provider", lambda: provider)


async def _stop(module: object, server: str, task: str) -> None:
    running = getattr(module, server)
    if running is not None:
        running.should_exit = True
        await asyncio.wait_for(getattr(module, task), timeout=10)
    setattr(module, server, None)
    setattr(module, task, None)


@pytest_asyncio.fixture
async def listeners() -> AsyncIterator[None]:
    yield
    await _stop(exec_broker, "_broker_server", "_broker_task")
    await _stop(llm_proxy, "_proxy_server", "_proxy_task")


_LISTENERS: dict[str, tuple[str, Callable[[], Awaitable[None]]]] = {
    "broker": ("OPENSCIENTIST_EXEC_BROKER_PORT", exec_broker.start_exec_broker),
    "proxy": ("OPENSCIENTIST_LLM_PROXY_PORT", llm_proxy.start_llm_proxy),
}


@pytest.mark.parametrize("listener", ["broker", "proxy"])
async def test_a_taken_port_stops_the_listener_naming_its_setting(
    listener: str,
    held_port: int,
    port_setting: Callable[[str, int], None],
    proxied_provider: None,
    listeners: None,
) -> None:
    setting, start = _LISTENERS[listener]
    port_setting(setting, held_port)
    with pytest.raises(ListenerBindError, match=f"{held_port}.*{setting}"):
        await start()


@pytest.mark.parametrize("listener", ["broker", "proxy"])
async def test_a_taken_port_stops_the_web_from_starting(
    listener: str,
    held_port: int,
    port_setting: Callable[[str, int], None],
    proxied_provider: None,
    listeners: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other startup failure is logged and the web carries on, but agent
    jobs cannot run without both listeners."""
    from fastapi import FastAPI

    from openscientist import skill_scheduler, web_app
    from openscientist.database import engine

    for name, (setting, _start) in _LISTENERS.items():
        port_setting(setting, held_port if name == listener else _free_port())
    monkeypatch.setattr(engine, "get_engine", lambda: None)
    monkeypatch.setattr(web_app, "_verify_db_connection_and_rls", AsyncMock())
    monkeypatch.setattr(web_app, "_ensure_default_skill_sources", AsyncMock())
    monkeypatch.setattr(skill_scheduler, "start_skill_scheduler", AsyncMock())

    with pytest.raises(ListenerBindError, match=str(held_port)):
        async with web_app._create_lifespan()(FastAPI()):
            pass


async def _answers(port: int) -> bool:
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            await client.get(f"http://127.0.0.1:{port}/")
        except httpx.TransportError:
            return False
    return True


@pytest.mark.parametrize("listener", ["broker", "proxy"])
async def test_a_listener_serves_on_its_configured_port(
    listener: str,
    port_setting: Callable[[str, int], None],
    proxied_provider: None,
    listeners: None,
) -> None:
    setting, start = _LISTENERS[listener]
    port = _free_port()
    port_setting(setting, port)
    await start()
    for _ in range(50):
        if await _answers(port):
            break
        await asyncio.sleep(0.1)
    assert await _answers(port)


@pytest.mark.parametrize(
    ("setting", "url"),
    [
        ("OPENSCIENTIST_EXEC_BROKER_PORT", container_broker_base_url),
        ("OPENSCIENTIST_LLM_PROXY_PORT", llm_proxy.container_proxy_base_url),
    ],
)
def test_agent_containers_are_sent_to_the_configured_port(
    setting: str,
    url: Callable[[], str],
    port_setting: Callable[[str, int], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_WEB_HOST", "web.internal")
    port_setting(setting, 18181)
    assert url() == "http://web.internal:18181"

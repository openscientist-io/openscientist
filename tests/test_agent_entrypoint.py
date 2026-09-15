"""Tests for docker/agent-entrypoint.py run-mode routing."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_PATH = REPO_ROOT / "docker" / "agent-entrypoint.py"


def _load_entrypoint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_entrypoint", ENTRYPOINT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _entrypoint_environ(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Copy process env, drop run-mode, then apply job vars (keeps PATH)."""
    env = {k: v for k, v in os.environ.items() if k != "OPENSCIENTIST_RUN_MODE"}
    env["JOB_ID"] = extra.pop("JOB_ID")
    env["JOB_DIR"] = str(tmp_path)
    env.update(extra)
    return env


def _stub_modules_for_routing(
    *,
    regenerate: AsyncMock,
    run_discovery: AsyncMock,
) -> dict[str, ModuleType]:
    """Stub discovery (and orchestrator package if needed) to avoid real orchestrator imports."""
    fake_discovery = types.ModuleType("openscientist.orchestrator.discovery")
    fake_discovery.regenerate_report_async = regenerate  # type: ignore[attr-defined]
    fake_discovery.run_discovery_async = run_discovery  # type: ignore[attr-defined]

    stubs: dict[str, ModuleType] = {
        "openscientist.orchestrator.discovery": fake_discovery,
    }
    # If the real package is not loaded yet, inject an empty package so importing
    # discovery does not execute orchestrator/__init__.py (which pulls in setup/magic).
    if "openscientist.orchestrator" not in sys.modules:
        pkg = types.ModuleType("openscientist.orchestrator")
        pkg.__path__ = []  # type: ignore[attr-defined]
        stubs["openscientist.orchestrator"] = pkg
    return stubs


@pytest.fixture
def entrypoint() -> ModuleType:
    return _load_entrypoint()


@pytest.mark.asyncio
async def test_main_routes_report_only_to_regenerate_report_async(
    entrypoint: ModuleType, tmp_path: Path
) -> None:
    regenerate = AsyncMock(return_value={"status": "completed", "iterations": 0, "findings": 0})
    run_discovery = AsyncMock(return_value={"status": "completed", "iterations": 1, "findings": 1})

    with (
        patch.dict(
            os.environ,
            _entrypoint_environ(
                tmp_path,
                JOB_ID="job-report-only",
                OPENSCIENTIST_RUN_MODE="report_only",
            ),
            clear=True,
        ),
        patch.dict(
            sys.modules,
            _stub_modules_for_routing(regenerate=regenerate, run_discovery=run_discovery),
        ),
    ):
        exit_code = await entrypoint.main()

    assert exit_code == 0
    regenerate.assert_awaited_once_with(tmp_path)
    run_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_routes_default_to_run_discovery_async(
    entrypoint: ModuleType, tmp_path: Path
) -> None:
    regenerate = AsyncMock(return_value={"status": "completed", "iterations": 0, "findings": 0})
    run_discovery = AsyncMock(return_value={"status": "completed", "iterations": 1, "findings": 1})

    # OPENSCIENTIST_RUN_MODE omitted so entrypoint default ("discovery") applies.
    with (
        patch.dict(
            os.environ,
            _entrypoint_environ(tmp_path, JOB_ID="job-discovery"),
            clear=True,
        ),
        patch.dict(
            sys.modules,
            _stub_modules_for_routing(regenerate=regenerate, run_discovery=run_discovery),
        ),
    ):
        exit_code = await entrypoint.main()

    assert exit_code == 0
    run_discovery.assert_awaited_once_with(tmp_path)
    regenerate.assert_not_awaited()

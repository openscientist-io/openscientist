"""Tests for thread-safe database engine singleton initialization."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import openscientist.database.engine as engine_mod

_CREATE_HOLD_TIMEOUT_S = 5.0
_RACE_WINDOW_TIMEOUT_S = 1.0


@pytest.fixture(autouse=True)
def _reset_engine_globals() -> Any:
    """Reset engine singletons before and after each test, even on failure."""
    engine_mod._engine = None
    engine_mod._admin_engine = None
    yield
    engine_mod._engine = None
    engine_mod._admin_engine = None


def _assert_concurrent_singleton(
    *,
    getter: Any,
    settings_url_attr: str,
    global_attr: str,
    thread_count: int = 8,
) -> None:
    """Drive concurrent first-use and prove create_async_engine runs once.

    The mock blocks inside ``create_async_engine`` until released. While it is
    blocked, any unlocked racer can also enter the mock. With correct locking,
    the entry count stays at 1 for the entire race window.
    """
    create_entries = 0
    entries_lock = threading.Lock()
    first_entered = threading.Event()
    release_create = threading.Event()
    start_barrier = threading.Barrier(thread_count)

    def _blocking_create(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal create_entries
        with entries_lock:
            create_entries += 1
            first_entered.set()
        if not release_create.wait(timeout=_CREATE_HOLD_TIMEOUT_S):
            raise TimeoutError(
                "Timed out waiting to release mocked create_async_engine; test coordination failed"
            )
        return MagicMock(name="AsyncEngine")

    def _worker() -> Any:
        start_barrier.wait()
        return getter()

    with (
        patch(
            "openscientist.database.engine.create_async_engine",
            side_effect=_blocking_create,
        ) as mock_create,
        patch("openscientist.database.engine.get_settings") as mock_settings,
    ):
        setattr(
            mock_settings.return_value.database,
            settings_url_attr,
            "postgresql+asyncpg://user:pass@localhost/db",
        )
        mock_settings.return_value.database.sql_echo = False

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(_worker) for _ in range(thread_count)]
            try:
                assert first_entered.wait(timeout=_CREATE_HOLD_TIMEOUT_S), (
                    "create_async_engine was never entered"
                )

                # While the first creator is held inside the mock, unlocked racers
                # would also enter. Wait the race window, then require a single entry.
                deadline = time.monotonic() + _RACE_WINDOW_TIMEOUT_S
                while time.monotonic() < deadline:
                    with entries_lock:
                        if create_entries > 1:
                            break
                    time.sleep(0.01)

                with entries_lock:
                    assert create_entries == 1, (
                        f"expected a single create_async_engine entrant during the "
                        f"race window, found {create_entries}"
                    )
            finally:
                release_create.set()

            results = [
                future.result(timeout=_CREATE_HOLD_TIMEOUT_S) for future in as_completed(futures)
            ]

    assert mock_create.call_count == 1
    assert len(results) == thread_count
    assert all(engine is results[0] for engine in results)
    assert getattr(engine_mod, global_attr) is results[0]


class TestEngineThreadSafeInitialization:
    """R15: concurrent first-use must create a single engine instance."""

    def test_concurrent_get_engine_creates_single_instance(self) -> None:
        _assert_concurrent_singleton(
            getter=engine_mod.get_engine,
            settings_url_attr="effective_database_url",
            global_attr="_engine",
        )

    def test_concurrent_get_admin_engine_creates_single_instance(self) -> None:
        _assert_concurrent_singleton(
            getter=engine_mod.get_admin_engine,
            settings_url_attr="effective_admin_database_url",
            global_attr="_admin_engine",
        )

    def test_app_and_admin_engines_are_independent_singletons(self) -> None:
        created: list[MagicMock] = []

        def _create(*_args: Any, **_kwargs: Any) -> MagicMock:
            engine = MagicMock(name=f"AsyncEngine-{len(created)}")
            created.append(engine)
            return engine

        with (
            patch(
                "openscientist.database.engine.create_async_engine",
                side_effect=_create,
            ) as mock_create,
            patch("openscientist.database.engine.get_settings") as mock_settings,
        ):
            mock_settings.return_value.database.effective_database_url = (
                "postgresql+asyncpg://app:pass@localhost/db"
            )
            mock_settings.return_value.database.effective_admin_database_url = (
                "postgresql+asyncpg://admin:pass@localhost/db"
            )
            mock_settings.return_value.database.sql_echo = False

            app_engine = engine_mod.get_engine()
            admin_engine = engine_mod.get_admin_engine()
            app_again = engine_mod.get_engine()
            admin_again = engine_mod.get_admin_engine()

        assert mock_create.call_count == 2
        assert app_engine is not admin_engine
        assert app_engine is app_again
        assert admin_engine is admin_again
        assert engine_mod._engine is app_engine
        assert engine_mod._admin_engine is admin_engine

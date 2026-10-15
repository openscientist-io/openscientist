"""Unit tests for `openscientist_tools.state.ToolServerState`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from openscientist_tools.state import ToolServerState


def _set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Set OPENSCIENTIST_* env vars hermetically (drop unrelated ones)."""
    for key in list(os.environ):
        if key.startswith("OPENSCIENTIST_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_required_fields_bound_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        OPENSCIENTIST_JOB_ID="abc-123",
        OPENSCIENTIST_JOB_DIR="/tmp/job",
    )
    state = ToolServerState()
    assert state.job_id == "abc-123"
    assert state.job_dir == Path("/tmp/job")
    assert state.data_file is None
    assert state.data_files == ()


def test_missing_required_field_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, OPENSCIENTIST_JOB_ID="abc-123")  # job_dir missing
    with pytest.raises(ValidationError) as excinfo:
        ToolServerState()
    assert "job_dir" in str(excinfo.value).lower()


def test_data_files_single_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        OPENSCIENTIST_JOB_ID="abc",
        OPENSCIENTIST_JOB_DIR="/tmp/job",
        OPENSCIENTIST_DATA_FILES="/data/a.csv",
    )
    state = ToolServerState()
    assert state.data_files == (Path("/data/a.csv"),)


def test_data_files_multiple_paths_split_on_pathsep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the env-var encoding contract that PR 13's launcher will produce."""
    joined = os.pathsep.join(["/data/a.csv", "/data/b.parquet", "/data/c.json"])
    _set_env(
        monkeypatch,
        OPENSCIENTIST_JOB_ID="abc",
        OPENSCIENTIST_JOB_DIR="/tmp/job",
        OPENSCIENTIST_DATA_FILES=joined,
    )
    state = ToolServerState()
    assert state.data_files == (
        Path("/data/a.csv"),
        Path("/data/b.parquet"),
        Path("/data/c.json"),
    )


def test_data_files_empty_string_yields_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(
        monkeypatch,
        OPENSCIENTIST_JOB_ID="abc",
        OPENSCIENTIST_JOB_DIR="/tmp/job",
        OPENSCIENTIST_DATA_FILES="",
    )
    state = ToolServerState()
    assert state.data_files == ()


def test_stale_use_hypotheses_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The removed opt-in's env var must not break an old launcher that still sets it."""
    _set_env(
        monkeypatch,
        OPENSCIENTIST_JOB_ID="abc",
        OPENSCIENTIST_JOB_DIR="/tmp/job",
        OPENSCIENTIST_USE_HYPOTHESES="1",
    )
    state = ToolServerState()
    assert not hasattr(state, "use_hypotheses")


def test_data_file_singular_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        OPENSCIENTIST_JOB_ID="abc",
        OPENSCIENTIST_JOB_DIR="/tmp/job",
        OPENSCIENTIST_DATA_FILE="/data/primary.csv",
    )
    state = ToolServerState()
    assert state.data_file == Path("/data/primary.csv")

"""Global pytest fixtures for SHANDY tests."""

import tempfile
from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_jobs_dir(temp_dir: Path) -> Path:
    """Create a temporary jobs directory structure."""
    jobs_dir = temp_dir / "jobs"
    jobs_dir.mkdir()
    return jobs_dir


@pytest.fixture
def sample_job_config() -> dict:
    """Sample job configuration for testing."""
    return {
        "job_id": "test_job_123",
        "research_question": "Test research question",
        "provider": "mock",
        "model": "mock-model",
        "coinvestigate": False,
        "status": "pending",
        "created_at": "2026-02-05T10:00:00",
    }


@pytest.fixture
def sample_knowledge_state() -> dict:
    """Sample knowledge state for testing."""
    return {
        "research_question": "Test research question",
        "iterations": [],
        "current_iteration": 0,
        "status": "pending",
        "plots": [],
        "literature": [],
        "datasets": [],
    }

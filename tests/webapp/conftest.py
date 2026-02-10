"""Pytest fixtures for webapp tests."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from .mocks import MockJobInfo, MockJobManager, MockProvider


@pytest.fixture(autouse=True)
def _nicegui_storage_dir(tmp_path):
    """Provide a temporary directory for NiceGUI storage during tests.

    NiceGUI persists user storage to .nicegui/ on disk. Without this,
    teardown fails with FileNotFoundError when async_backup runs.
    """
    storage_dir = tmp_path / ".nicegui"
    storage_dir.mkdir()
    with patch.dict(os.environ, {"NICEGUI_STORAGE_PATH": str(storage_dir)}):
        yield


@pytest.fixture(autouse=True)
def _disable_auth():
    """Disable authentication for all webapp tests.

    The require_auth decorator reads DISABLE_AUTH from shandy.auth.middleware.
    """
    with patch("shandy.auth.middleware.DISABLE_AUTH", True):
        yield


@pytest.fixture
def mock_job_manager(temp_jobs_dir: Path) -> MockJobManager:
    """Create a mock job manager for testing."""
    return MockJobManager(jobs_dir=str(temp_jobs_dir))


@pytest.fixture
def mock_provider() -> MockProvider:
    """Create a mock provider for testing."""
    return MockProvider()


@pytest.fixture
def sample_job_info() -> MockJobInfo:
    """Create sample job info for testing."""
    return MockJobInfo(
        job_id="test_job_123",
        status="completed",
        research_question="What is the effect of X on Y?",
        created_at="2026-02-05T10:00:00",
        provider="mock",
        model="mock-model",
    )


@pytest.fixture
def sample_job_with_error() -> MockJobInfo:
    """Create sample job info with error for testing."""
    return MockJobInfo(
        job_id="error_job_456",
        status="failed",
        research_question="Test question",
        created_at="2026-02-05T10:00:00",
        error='{"type": "error", "error": {"type": "api_error", "message": "API request failed"}}',
    )

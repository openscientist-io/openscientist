"""Pytest fixtures for webapp tests."""

from pathlib import Path

import pytest

from .mocks import MockJobInfo, MockJobManager, MockProvider


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

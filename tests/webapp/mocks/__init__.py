"""Mock objects and fixtures for webapp testing."""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class MockJobInfo:
    """Mock job information."""

    job_id: str
    status: str
    research_question: str
    created_at: str
    error: Optional[str] = None
    provider: str = "mock"
    model: str = "mock-model"
    coinvestigate: bool = False
    iterations_completed: int = 5
    max_iterations: int = 10
    findings_count: int = 3
    failed_at: Optional[str] = None
    completed_at: Optional[str] = None

    def __post_init__(self):
        """Convert status string to enum-compatible object."""
        from shandy.job_manager import JobStatus

        if isinstance(self.status, str):
            # Try to convert to actual JobStatus enum
            try:
                self.status = JobStatus(self.status)
            except ValueError:
                # If not a valid enum value, create a simple object with .value attribute
                class StatusValue:
                    def __init__(self, value):
                        self.value = value

                    def __eq__(self, other):
                        if hasattr(other, "value"):
                            return self.value == other.value
                        return self.value == str(other)

                self.status = StatusValue(self.status)


@dataclass
class MockProvider:
    """Mock provider for testing."""

    name: str = "mock"
    model: str = "mock-model"

    def get_total_cost(self) -> float:
        """Return mock cost."""
        return 1.50

    def get_budget_info(self) -> dict[str, Any]:
        """Return mock budget info."""
        return {
            "total_cost": 1.50,
            "budget_limit": 100.0,
            "budget_remaining": 98.50,
            "warnings": [],
        }

    def get_cost_info(self, lookback_hours: int = 24):
        """Return mock cost info."""
        cost_info = type("CostInfo", (), {})()
        cost_info.total_spend_usd = 15.50
        cost_info.recent_spend_usd = 2.25
        cost_info.recent_period_hours = lookback_hours
        cost_info.budget_remaining_usd = 84.50
        cost_info.provider_name = self.name
        cost_info.description = f"{self.name} costs"
        cost_info.data_lag_note = None
        return cost_info

    def check_budget_limits(self) -> dict[str, Any]:
        """Return mock budget check."""
        return {
            "within_budget": True,
            "warnings": [],
            "errors": [],
        }


class MockJobManager:
    """Mock JobManager for testing."""

    def __init__(self, jobs_dir: str = "/tmp/jobs"):
        from pathlib import Path

        self.jobs_dir = Path(jobs_dir) if isinstance(jobs_dir, str) else jobs_dir
        self._jobs: dict[str, MockJobInfo] = {}

    def list_jobs(self) -> list[MockJobInfo]:
        """List all jobs."""
        return list(self._jobs.values())

    def get_job_summary(self) -> dict[str, Any]:
        """Get summary of jobs by status."""
        status_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
        for job in self._jobs.values():
            status = job.status if isinstance(job.status, str) else job.status.value
            if status in status_counts:
                status_counts[status] += 1
        return {"total_jobs": len(self._jobs), "status_counts": status_counts}

    def get_job_info(self, job_id: str) -> Optional[MockJobInfo]:
        """Get job information."""
        return self._jobs.get(job_id)

    def get_job(self, job_id: str) -> Optional[MockJobInfo]:
        """Get job information (alias for get_job_info)."""
        return self._jobs.get(job_id)

    def create_job(self, job_id: str, config: dict) -> None:
        """Create a new job."""
        self._jobs[job_id] = MockJobInfo(
            job_id=job_id,
            status="pending",
            research_question=config.get("research_question", ""),
            created_at=config.get("created_at", "2026-02-05T10:00:00"),
            provider=config.get("provider", "mock"),
            model=config.get("model", "mock-model"),
            coinvestigate=config.get("coinvestigate", False),
        )

    def add_job(self, job_info: MockJobInfo) -> None:
        """Add a job to the manager."""
        self._jobs[job_info.job_id] = job_info

    def load_knowledge_state(self, job_id: str) -> dict:
        """Load knowledge state for a job."""
        return {
            "research_question": "Test question",
            "iterations": [],
            "current_iteration": 0,
            "status": "pending",
        }


# Sample error messages for testing
SAMPLE_ERRORS = {
    "config_error": (
        "Configuration error: Missing required environment variable ANTHROPIC_API_KEY"
    ),
    "provider_error": (
        '{"type": "error", "error": {"type": "authentication_error", "message": "Invalid API key"}}'
    ),
    "budget_error": ("Budget exceeded: Current cost $105.50 exceeds budget limit $100.00"),
    "rate_limit_error": ('{"error": {"message": "Rate limit exceeded. Please try again later."}}'),
    "gcp_credentials_error": (
        "google.auth.exceptions.DefaultCredentialsError: Could not find credentials"
    ),
}

# Sample transcript data for testing
SAMPLE_TRANSCRIPT = {
    "iterations": [
        {
            "actions": [
                {
                    "tool": {
                        "name": "shandy_load_data",
                        "input": {"path": "data.csv"},
                    },
                    "result": {
                        "success": True,
                        "data": '{"shape": [100, 5]}',
                    },
                },
                {
                    "tool": {
                        "name": "shandy_analyze",
                        "input": {"method": "correlation"},
                    },
                    "result": {
                        "success": False,
                        "error": "Invalid method",
                    },
                },
            ]
        }
    ]
}

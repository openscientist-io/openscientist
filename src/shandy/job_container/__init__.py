"""
Job container management for SHANDY.

Each agent job runs in its own Docker container:
- runner.py — JobContainerRunner: launch/stop/cleanup containers
"""

from shandy.job_container.runner import JobContainerRunner

__all__ = ["JobContainerRunner"]

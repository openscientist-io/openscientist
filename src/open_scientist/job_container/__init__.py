"""
Job container management for Open Scientist.

Each agent job runs in its own Docker container:
- runner.py — JobContainerRunner: launch/stop/cleanup containers
"""

from open_scientist.job_container.runner import JobContainerRunner

__all__ = ["JobContainerRunner"]

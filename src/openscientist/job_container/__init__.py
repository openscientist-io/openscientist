"""
Job container management for OpenScientist.

Each agent job runs in its own Docker container:
- runner.py — JobContainerRunner: launch/stop/cleanup containers
"""

from openscientist.job_container.runner import JobContainerRunner

__all__ = ["JobContainerRunner"]

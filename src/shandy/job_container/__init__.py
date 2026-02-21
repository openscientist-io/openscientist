"""
Job container management for SHANDY.

Each agent job (Anthropic provider) runs in its own Docker container:
- runner.py  — JobContainerRunner: launch/stop/cleanup containers
- monitor.py — ContainerMonitor: polls DB for terminal status
"""

from shandy.job_container.monitor import ContainerMonitor
from shandy.job_container.runner import JobContainerRunner

__all__ = ["ContainerMonitor", "JobContainerRunner"]

"""
Job container management for OpenScientist.

Each agent job runs in its own Docker container:
- runner.py — JobContainerRunner: launch/stop/cleanup containers
"""

from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_container.utils import resolve_docker_network, to_host_path

__all__ = ["JobContainerRunner", "resolve_docker_network", "to_host_path"]

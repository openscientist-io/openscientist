"""
Job container management for OpenScientist.

Each agent job runs in its own Docker container:
- runner.py — JobContainerRunner: launch/stop/cleanup containers
"""

from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_container.secrets import derive_job_secret
from openscientist.job_container.utils import to_host_path

__all__ = ["JobContainerRunner", "derive_job_secret", "to_host_path"]

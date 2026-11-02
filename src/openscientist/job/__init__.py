"""
Job management subpackage for OpenScientist.

Module layout:
- types.py — JobStatus, JobInfo, JobStatusUpdateResult
- status.py — the guarded status UPDATE both writers of a job's status share
- repository.py — the job table's rows the web process reads and writes
"""

from openscientist.job.types import JobInfo, JobStatus

__all__ = ["JobInfo", "JobStatus"]

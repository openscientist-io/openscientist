"""
Job management subpackage for OpenScientist.

Module layout:
- types.py — JobStatus, JobInfo, JobStatusUpdateResult
- status.py — the guarded status UPDATE both writers of a job's status share
"""

from openscientist.job.types import JobInfo, JobStatus

__all__ = ["JobInfo", "JobStatus"]

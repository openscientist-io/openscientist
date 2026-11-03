"""Job-directory path confinement for the file tools.

The tools MCP server runs in the agent container, which also mounts
credential and Phenix paths alongside the read-write job directory;
``execute_code`` runs in a separate executor container instead. Any
path taken from the model must be confined to the job directory before
it is used.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class OutsideJobDirError(ValueError):
    """A tool-supplied path is not a regular file inside the job directory."""


def resolve_job_file(candidate: Path, job_dir: Path) -> Path:
    """Return *candidate* as a path confined to *job_dir*, or raise
    :class:`OutsideJobDirError`.

    Symlinked components are followed only while the resolution stays
    inside the job directory. An existing leaf is opened with
    ``O_NOFOLLOW`` and required to be a regular file so a symlink
    planted after resolution is refused at open time.
    """
    root = job_dir.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise OutsideJobDirError(str(candidate))
    if resolved.exists():
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OutsideJobDirError(str(candidate))
        finally:
            os.close(fd)
    return resolved

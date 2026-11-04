"""Fixed container identities and the ownership of the job tree they write.

The web process runs as its own non-root uid in the jobs group and creates the
job tree, which the agent and executor containers read and write as their
fixed non-root users. The constants below are the single source of truth for
those ids, and the Dockerfile.agent and Dockerfile.executor build args must
match them (the tests in tests/test_job_dir_permissions.py keep them in sync).

Job dirs are setgid, so every file created inside one inherits the jobs
group no matter which process creates it. A web in the jobs group needs no
chown for anything it writes, and the chown below only runs as root.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

#: UID and GID of the agent container's user.
AGENT_UID = 1001
AGENT_GID = 1001
#: UID of the executor container's user. It shares AGENT_GID, so it can read
#: a job's data and write its provenance.
EXECUTOR_UID = 1000

#: Job tree, setgid so created files inherit the jobs group, no world bits.
JOB_DIR_MODE = 0o2770
JOB_FILE_MODE = 0o660
#: Root hands run homes to the agent owner-only, holding the operator's login.
RUN_HOME_DIR_MODE = 0o700
#: A non-root web cannot chown the home to the agent, so the home takes the
#: agent's access from the jobs group instead. No world bits.
RUN_HOME_GROUP_MODE = 0o770
PRIVATE_FILE_MODE = 0o600


def apply_job_ownership(
    path: Path, *, dir_mode: int = JOB_DIR_MODE, file_mode: int = JOB_FILE_MODE
) -> None:
    """Hand one file or directory the web just created to the containers.

    Inside a setgid job dir the path already carries the jobs group, which
    makes the chown redundant. The deployed web is non-root, so it relies on
    that group membership and only the chmod runs for it.
    """
    if os.geteuid() == 0 and path.stat().st_gid != AGENT_GID:
        with suppress(OSError):
            os.chown(path, AGENT_UID, AGENT_GID)
    path.chmod(dir_mode if path.is_dir() else file_mode)


def apply_job_ownership_tree(root: Path) -> None:
    """Hand the initial tree the web just created to the containers, such as
    the repository that ``git init`` leaves behind.

    Git refuses a repository whose ``.git`` dir the current user does not
    own, so the whole tree is chowned even where the setgid dir already
    carries the jobs group.
    """
    paths = list(root.rglob("*")) + [root]
    if os.geteuid() == 0:
        for path in paths:
            with suppress(OSError):
                os.chown(path, AGENT_UID, AGENT_GID)
    for path in paths:
        with suppress(OSError):
            path.chmod(JOB_DIR_MODE if path.is_dir() else JOB_FILE_MODE)

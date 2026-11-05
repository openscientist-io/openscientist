"""Homes of single container runs, where a harness keeps the operator's login.

A harness that logs in with the operator's own account (codex with a ChatGPT
login, omp with its credential vault) needs that login while its container
runs. Each run gets a home of its own under the jobs root, named after its
database login, mounted as the harness's home inside the container, and
removed with the login, so overlapping runs of a job never share a copy and no
copy outlives its run.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path

from openscientist.job_container.identity import (
    RUN_HOME_DIR_MODE,
    RUN_HOME_GROUP_MODE,
    apply_job_ownership,
)

logger = logging.getLogger(__name__)

RUN_HOMES_DIR = ".run-homes"

#: Container label that carries the run's home on the web's side.
RUN_HOME_LABEL = "openscientist.run_home"

#: Env the launcher names the run home's mount to the harness, outside the
#: job mount so the daemon's mountpoint never lands in the job directory.
RUN_HOME_ENV = "OPENSCIENTIST_RUN_HOME"

# Where harnesses kept their homes inside the job directory before each run
# had its own. Copies of the operator's login may still sit there.
_LEGACY_JOB_HOMES = (".codex", ".omp-home")


def run_home_path(jobs_dir: Path, role: str) -> Path:
    """The home of the run whose database login is ``role``."""
    return jobs_dir / RUN_HOMES_DIR / role


def create_run_home(jobs_dir: Path, role: str) -> Path:
    """Create the empty home of the run whose database login is ``role``.

    Root hands the home to the agent user owner-only. A non-root web
    cannot chown, so home and parent open their group bits to the jobs
    group, which the setgid tree already carries.
    """
    home = run_home_path(jobs_dir, role)
    home_mode = RUN_HOME_DIR_MODE if os.geteuid() == 0 else RUN_HOME_GROUP_MODE
    home.parent.mkdir(parents=True, exist_ok=True)
    apply_job_ownership(home.parent, dir_mode=home_mode)
    home.mkdir()
    apply_job_ownership(home, dir_mode=home_mode)
    return home


def remove_run_home(home: Path) -> None:
    """Remove a run's home and the login in it. Only logs a failure, because it
    runs during teardown, where the reaper retries."""
    if home.parent.name != RUN_HOMES_DIR:
        logger.error("Refusing to remove %s, which is not a run home", home)
        return
    try:
        shutil.rmtree(home)
    except FileNotFoundError:
        return
    except OSError as error:
        logger.error("Could not remove run home %s: %s", home, error)


def remove_stale_run_homes(
    jobs_dir: Path, live_roles: set[str] | None, *, older_than: timedelta
) -> list[str]:
    """Remove the homes of runs whose login no listed container carries, older
    than ``older_than`` so a launch in progress keeps its home, and return their
    roles. ``live_roles`` is None when Docker cannot be asked, and then nothing
    is removed."""
    root = jobs_dir / RUN_HOMES_DIR
    if live_roles is None or not root.is_dir():
        return []
    cutoff = time.time() - older_than.total_seconds()
    removed = []
    for home in sorted(root.iterdir()):
        if home.name in live_roles or home.stat().st_mtime > cutoff:
            continue
        remove_run_home(home)
        removed.append(home.name)
    if removed:
        logger.info("Removed %d run homes no container needs", len(removed))
    return removed


def remove_legacy_job_homes(jobs_dir: Path, live_job_ids: set[str] | None) -> list[str]:
    """Remove the harness homes past runs left inside job directories, with the
    operator logins in them, from every job no listed container runs, and return
    those jobs. ``live_job_ids`` is None when Docker cannot be asked, and then
    nothing is removed."""
    if live_job_ids is None or not jobs_dir.is_dir():
        return []
    cleaned = []
    for job_dir in sorted(jobs_dir.iterdir()):
        if job_dir.name == RUN_HOMES_DIR or job_dir.name in live_job_ids:
            continue
        legacy = [job_dir / name for name in _LEGACY_JOB_HOMES if (job_dir / name).is_dir()]
        for home in legacy:
            try:
                shutil.rmtree(home)
            except OSError as error:
                logger.error("Could not remove %s: %s", home, error)
        if legacy:
            cleaned.append(job_dir.name)
    if cleaned:
        logger.info("Removed harness homes left in %d job directories", len(cleaned))
    return cleaned

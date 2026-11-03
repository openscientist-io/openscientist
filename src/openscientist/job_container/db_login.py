"""Database logins for agent containers.

Every agent container gets its own login, a member of openscientist_agent mapped
to its job in agent_job_roles, so the policies hold it to that job's rows. The
login is created right before the container starts and dropped when the
container is removed. It expires on its own a little after the longest the
container can run, so a login that teardown misses stops working anyway, and
the reaper drops what is left.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url

from openscientist.async_tasks import run_sync
from openscientist.database.session import async_session_local
from openscientist.job_container.run_home import remove_legacy_job_homes, remove_stale_run_homes
from openscientist.job_container.secrets import revoke_run_credentials
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

#: Container label that carries the container's login name.
AGENT_LOGIN_LABEL = "openscientist.db_login"

_ROLE_PATTERN = re.compile(r"agent_[0-9a-f]{32}")
# Room for teardown and clock skew past the container's own deadline.
_GRACE = timedelta(minutes=10)
# How long a mapped login may wait for its container to appear. Launch can
# include an image pull between creating the login and starting the container.
_LAUNCH_GRACE = timedelta(minutes=15)
#: Seconds between reaper runs in the web process.
REAP_INTERVAL_SECONDS = 600


@dataclass(frozen=True)
class AgentLogin:
    role: str
    database_url: str


#: PostgreSQL's default ``scram_iterations``.
_SCRAM_ITERATIONS = 4096


def _scram_verifier(password: str) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for ``password`` (RFC 7677),
    which accepts the password at login but cannot be turned back into it."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _SCRAM_ITERATIONS)
    stored_key = hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode()

    return f"SCRAM-SHA-256${_SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


async def create_agent_login(job_id: str, *, lifetime_seconds: int) -> AgentLogin:
    """Create a login for one container of ``job_id`` and map it to the job."""
    role = f"agent_{secrets.token_hex(16)}"
    password = secrets.token_urlsafe(32)
    valid_until = datetime.now(UTC) + timedelta(seconds=lifetime_seconds) + _GRACE
    async with async_session_local() as session:
        # CREATE ROLE takes no bind parameters, so the server quotes the values.
        # The statement is SQL text that errors and the server log repeat, so it
        # carries the password's SCRAM verifier, never the password.
        statement = (
            await session.execute(
                text(
                    "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L VALID UNTIL %L "
                    "IN ROLE openscientist_agent', CAST(:role AS text), "
                    "CAST(:verifier AS text), CAST(:valid_until AS text))"
                ),
                {
                    "role": role,
                    "verifier": _scram_verifier(password),
                    "valid_until": valid_until.isoformat(),
                },
            )
        ).scalar_one()
        await (await session.connection()).exec_driver_sql(statement)
        await session.execute(
            text("INSERT INTO agent_job_roles (role_name, job_id) VALUES (:role, :job)"),
            {"role": role, "job": UUID(job_id)},
        )
        await session.commit()
    url = make_url(get_settings().database.effective_database_url)
    url = url.set(username=role, password=password)
    return AgentLogin(role=role, database_url=url.render_as_string(hide_password=False))


async def drop_agent_login(role: str) -> None:
    """End the run's LLM proxy and broker credentials, then unmap the login, end
    its sessions and drop it.

    The credentials and the mapping go first, so the run reaches nothing even if
    a later step fails.
    """
    if not _ROLE_PATTERN.fullmatch(role):
        raise ValueError(f"{role!r} is not an agent login")
    revoke_run_credentials(role)
    async with async_session_local() as session:
        await session.execute(
            text("DELETE FROM agent_job_roles WHERE role_name = :role"), {"role": role}
        )
        await session.commit()
        await session.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :role"),
            {"role": role},
        )
        statement = (
            await session.execute(
                text("SELECT format('DROP ROLE IF EXISTS %I', CAST(:role AS text))"),
                {"role": role},
            )
        ).scalar_one()
        await (await session.connection()).exec_driver_sql(statement)
        await session.commit()


async def reap_agent_logins(live_roles: set[str] | None) -> list[str]:
    """Drop agent logins that no container needs any more, and return them.

    ``live_roles`` holds the logins of every container Docker lists, or is None
    when Docker cannot be asked, in which case only expired and unmapped logins
    are dropped. Only roles named like an agent login that are members of
    openscientist_agent are considered.
    """
    async with async_session_local() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT r.rolname,
                           r.rolvaliduntil < now() AS expired,
                           m.created_at AS mapped_at
                    FROM pg_catalog.pg_roles r
                    JOIN pg_catalog.pg_auth_members am ON am.member = r.oid
                    JOIN pg_catalog.pg_roles g
                      ON g.oid = am.roleid AND g.rolname = 'openscientist_agent'
                    LEFT JOIN agent_job_roles m ON m.role_name = r.rolname
                    WHERE r.rolname ~ '^agent_[0-9a-f]{32}$'
                    """
                )
            )
        ).all()
    launch_cutoff = datetime.now(UTC) - _LAUNCH_GRACE
    stale = [
        row.rolname
        for row in rows
        if row.expired
        or row.mapped_at is None
        or (
            live_roles is not None
            and row.rolname not in live_roles
            and row.mapped_at < launch_cutoff
        )
    ]
    dropped = []
    for role in stale:
        try:
            await drop_agent_login(role)
        except Exception as error:
            logger.error("Could not reap agent database login %s: %s", role, error)
        else:
            dropped.append(role)
    if dropped:
        logger.info("Reaped %d leftover agent database logins", len(dropped))
    return dropped


async def reap_leftover_agent_logins(jobs_dir: Path) -> list[str]:
    """One reaper run, with the live set taken from Docker. It also removes the
    run homes no container needs and the harness homes past runs left inside
    job directories."""
    # Imported here because the runner imports this module.
    from openscientist.job_container.runner import JobContainerRunner

    def live() -> tuple[set[str] | None, set[str] | None]:
        try:
            runner = JobContainerRunner()
            return runner.live_login_roles(), runner.live_job_ids()
        except Exception as error:
            logger.warning("Docker unavailable to the login reaper: %s", error)
            return None, None

    live_roles, live_jobs = await asyncio.to_thread(live)
    dropped = await reap_agent_logins(live_roles)

    def sweep() -> None:
        remove_stale_run_homes(jobs_dir, live_roles, older_than=_LAUNCH_GRACE)
        remove_legacy_job_homes(jobs_dir, live_jobs)

    await asyncio.to_thread(sweep)
    return dropped


async def run_agent_login_reaper(
    jobs_dir: Path, interval_seconds: int = REAP_INTERVAL_SECONDS
) -> None:
    """Reap at once, then every ``interval_seconds``, for the web's lifetime."""
    while True:
        try:
            await reap_leftover_agent_logins(jobs_dir)
        except Exception as error:
            logger.error("Agent login reaper run failed: %s", error)
        await asyncio.sleep(interval_seconds)


def open_agent_login(job_id: str, *, lifetime_seconds: int) -> AgentLogin:
    """Synchronous :func:`create_agent_login`, for the container runner."""
    try:
        return run_sync(create_agent_login(job_id, lifetime_seconds=lifetime_seconds))
    except Exception as error:
        raise RuntimeError(
            f"Could not create the database login for an agent container of job {job_id}: {error}"
        ) from error


def close_agent_login(role: str) -> None:
    """Synchronous :func:`drop_agent_login` that only logs a failure, because it
    runs during teardown. The login still expires on its own."""
    try:
        run_sync(drop_agent_login(role))
    except Exception as error:
        logger.error("Could not drop agent database login %s: %s", role, error)

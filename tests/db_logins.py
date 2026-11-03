"""The database logins conftest bootstraps, for tests that log in as one of them."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from openscientist.settings import get_settings

BOOTSTRAP_SQL = Path(__file__).parent.parent / "docker" / "postgres" / "bootstrap.sql"
#: Where the testcontainer mounts ``docker/postgres``.
CONTAINER_BOOTSTRAP_DIR = "/bootstrap"

OWNER_PASSWORD = "test-owner-password"
WEB_PASSWORD = "test-web-password"
ADMIN_PASSWORD = "test-admin-password"
BOOTSTRAP_ENV = {
    "POSTGRES_OWNER_PASSWORD": OWNER_PASSWORD,
    "POSTGRES_WEB_PASSWORD": WEB_PASSWORD,
    "POSTGRES_ADMIN_PASSWORD": ADMIN_PASSWORD,
}


def login_url(database_url: str, role: str, password: str) -> str:
    return (
        make_url(database_url)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


def run_bootstrap(
    database_url: str, container: PostgresContainer | None, env: Mapping[str, str]
) -> tuple[int, str]:
    """Run ``bootstrap.sql`` as the superuser of ``database_url`` and return its exit
    code and output. ``env`` carries the passwords, as compose passes them."""
    url = make_url(database_url)
    args = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", str(url.username)]
    args += ["-d", str(url.database)]
    if container is not None:
        exit_code, output = container.get_wrapped_container().exec_run(
            [*args, "-f", f"{CONTAINER_BOOTSTRAP_DIR}/bootstrap.sql"], environment=dict(env)
        )
        return exit_code, output.decode()
    result = subprocess.run(
        [*args, "-h", str(url.host), "-p", str(url.port), "-f", str(BOOTSTRAP_SQL)],
        env={**os.environ, "PGPASSWORD": url.password or "", **env},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def reset_database_state() -> None:
    """Forget cached settings, so the next session reads the URLs again."""
    get_settings.cache_clear()

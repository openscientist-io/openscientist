"""A checkout from before the env split upgrades in place, keeping the database
superuser password the existing volume was created with."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import TypedDict

import pytest
import yaml
from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles, stack, wizard
from tests.cli.conftest import REPO_ROOT

runner = CliRunner()

SUPERUSER_PASSWORD = "old-superuser-password"
SECRET_KEY = "5e" * 32
API_KEY = "sk-ant-api03-old-key"
GOOGLE_SECRET = "old-google-secret"
SUPERUSER_URL = (
    f"postgresql+asyncpg://openscientist:{SUPERUSER_PASSWORD}@localhost:5432/openscientist"
)

#: A single .env as checkouts held it before .env.db and .env.owner existed.
LEGACY_ENV = f"""\
# OpenScientist configuration
CLAUDE_PROVIDER=anthropic
ANTHROPIC_MODEL=claude-sonnet-4-5  # the default model
ANTHROPIC_API_KEY={API_KEY}
POSTGRES_USER=openscientist
POSTGRES_PASSWORD={SUPERUSER_PASSWORD}
POSTGRES_DB=openscientist
DATABASE_URL={SUPERUSER_URL}
ADMIN_DATABASE_URL={SUPERUSER_URL}
OPENSCIENTIST_SECRET_KEY={SECRET_KEY}
GOOGLE_CLIENT_ID=old-google-client-id
GOOGLE_CLIENT_SECRET={GOOGLE_SECRET}
GITHUB_CLIENT_ID=old-github-client-id
GITHUB_CLIENT_SECRET=old-github-secret
DATA_DIR=/app/data
JOBS_DIR=/app/jobs
CLEAN_STORAGE=false
CLEAN_PLOTS=true
TOKEN_ENCRYPTION_KEY=old-token-key
SKILLS_ENABLED=true
ALLOW_SKILLS_TOGGLE=true
ORCID_API_BASE=https://pub.orcid.org/v3.0
PORT=8090
"""

DROPPED = (
    "DATABASE_URL",
    "ADMIN_DATABASE_URL",
    "DATA_DIR",
    "JOBS_DIR",
    "CLEAN_STORAGE",
    "CLEAN_PLOTS",
    "TOKEN_ENCRYPTION_KEY",
    "SKILLS_ENABLED",
    "ALLOW_SKILLS_TOGGLE",
    "ORCID_API_BASE",
)


def _legacy(checkout: Path, text: str = LEGACY_ENV) -> None:
    (checkout / ".env").write_text(text, encoding="utf-8")


def test_a_legacy_env_splits_into_the_three_files(checkout: Path) -> None:
    _legacy(checkout)
    assert not envfiles.is_configured(checkout)

    envfiles.split_legacy_env(checkout)

    env = envfiles.file_values(checkout, "env")
    db = envfiles.file_values(checkout, "db")
    owner = envfiles.file_values(checkout, "owner")
    assert envfiles.is_configured(checkout)
    assert db["POSTGRES_PASSWORD"] == SUPERUSER_PASSWORD
    assert env["OPENSCIENTIST_SECRET_KEY"] == SECRET_KEY
    assert owner["POSTGRES_OWNER_PASSWORD"]
    for key in ("POSTGRES_WEB_PASSWORD", "POSTGRES_ADMIN_PASSWORD", "LAUNCHER_INSTANCE_ID"):
        assert env[key], key
    assert env["OPENSCIENTIST_PROVIDER"] == "anthropic"
    assert env["OPENSCIENTIST_MODEL"] == "claude-sonnet-4-5"
    for key, value in {
        "ANTHROPIC_API_KEY": API_KEY,
        "GOOGLE_CLIENT_SECRET": GOOGLE_SECRET,
        "GITHUB_CLIENT_ID": "old-github-client-id",
        "POSTGRES_USER": "openscientist",
        "POSTGRES_DB": "openscientist",
        "PORT": "8090",
    }.items():
        assert env[key] == value, key
    assert not {"CLAUDE_PROVIDER", "ANTHROPIC_MODEL", "POSTGRES_PASSWORD", *DROPPED} & env.keys()
    # The web loads .env, so the superuser password must not survive there in any line.
    assert SUPERUSER_PASSWORD not in (checkout / ".env").read_text(encoding="utf-8")


def test_the_original_is_backed_up_and_every_file_is_owner_only(checkout: Path) -> None:
    _legacy(checkout)
    envfiles.split_legacy_env(checkout)

    [backup] = checkout.glob(".env.pre-split-*")
    assert backup.read_text(encoding="utf-8") == LEGACY_ENV
    for path in (backup, checkout / ".env", checkout / ".env.db", checkout / ".env.owner"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_a_provider_and_model_already_under_their_new_names_win(checkout: Path) -> None:
    _legacy(checkout, LEGACY_ENV + "OPENSCIENTIST_PROVIDER=vertex\nOPENSCIENTIST_MODEL=gemini\n")
    envfiles.split_legacy_env(checkout)

    env = envfiles.file_values(checkout, "env")
    assert (env["OPENSCIENTIST_PROVIDER"], env["OPENSCIENTIST_MODEL"]) == ("vertex", "gemini")
    assert not {"CLAUDE_PROVIDER", "ANTHROPIC_MODEL"} & env.keys()


@pytest.mark.parametrize("base_url_assignment", ["", "OPENSCIENTIST_BASE_URL=\n"])
def test_a_legacy_app_url_supplies_the_stack_base_url(
    checkout: Path, base_url_assignment: str
) -> None:
    _legacy(
        checkout,
        LEGACY_ENV + "APP_URL=https://scientist.example.org\n" + base_url_assignment,
    )

    changes = envfiles.split_legacy_env(checkout)

    values = envfiles.file_values(checkout, "env")
    assert values["OPENSCIENTIST_BASE_URL"] == "https://scientist.example.org"
    assert values["APP_URL"] == "https://scientist.example.org"
    assert "Copied APP_URL to OPENSCIENTIST_BASE_URL." in changes


def test_an_existing_base_url_wins_over_the_legacy_app_url(checkout: Path) -> None:
    _legacy(
        checkout,
        LEGACY_ENV
        + "APP_URL=https://scientist.example.org\n"
        + "OPENSCIENTIST_BASE_URL=https://research.example.org\n",
    )

    changes = envfiles.split_legacy_env(checkout)

    values = envfiles.file_values(checkout, "env")
    assert values["OPENSCIENTIST_BASE_URL"] == "https://research.example.org"
    assert values["APP_URL"] == "https://scientist.example.org"
    assert "Copied APP_URL to OPENSCIENTIST_BASE_URL." not in changes


def test_a_split_checkout_is_left_alone(checkout: Path) -> None:
    _legacy(checkout)
    (checkout / ".env.db").write_text("POSTGRES_PASSWORD=split\n", encoding="utf-8")

    envfiles.split_legacy_env(checkout)

    assert (checkout / ".env").read_text(encoding="utf-8") == LEGACY_ENV
    assert not list(checkout.glob(".env.pre-split-*"))
    assert not (checkout / ".env.owner").exists()


def test_a_split_interrupted_writing_env_db_completes_on_the_next_run(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed .env.db write must not leave the superuser password only in the
    backup, where the next run would miss it and generate another."""
    _legacy(checkout)

    def disk_full(*args: object) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched:
        patched.setattr(envfiles, "set_file_value", disk_full)
        with pytest.raises(OSError):
            envfiles.split_legacy_env(checkout)
    envfiles.split_legacy_env(checkout)

    assert envfiles.file_values(checkout, "db")["POSTGRES_PASSWORD"] == SUPERUSER_PASSWORD
    assert envfiles.is_configured(checkout)


def test_start_on_a_legacy_checkout_skips_the_wizard_and_names_only_keys(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv run openscientist` upgrades the files, then starts the stack without
    asking anything, and its report holds key names, never values."""
    _legacy(checkout)
    started: list[Path] = []

    def wizard_ran(*args: object, **kwargs: object) -> None:
        raise AssertionError("the wizard ran on a legacy checkout")

    def start(root: Path, run: object) -> int:
        started.append(root)
        return 0

    monkeypatch.setattr(wizard, "run_setup", wizard_ran)
    monkeypatch.setattr(stack, "start", start)
    result = runner.invoke(cli_app.app, [])

    assert result.exit_code == 0, result.output
    assert started == [checkout]
    for key in ("POSTGRES_PASSWORD", "CLAUDE_PROVIDER", "DATABASE_URL", "POSTGRES_WEB_PASSWORD"):
        assert key in result.output, key
    env = envfiles.file_values(checkout, "env")
    for secret in (SUPERUSER_PASSWORD, SECRET_KEY, API_KEY, env["POSTGRES_WEB_PASSWORD"]):
        assert secret not in result.output


# --- the existing database accepts the carried password ------------------------


class _ComposeService(TypedDict):
    """The db-bootstrap fields the test runs it from."""

    image: str
    entrypoint: list[str]


def _db_bootstrap_service() -> _ComposeService:
    compose = yaml.safe_load((REPO_ROOT / stack.COMPOSE_FILE).read_text(encoding="utf-8"))
    service: _ComposeService = compose["services"]["db-bootstrap"]
    return service


def _run_db_bootstrap(
    checkout: Path, postgres_address: str, superuser_password: str
) -> tuple[int, str]:
    """db-bootstrap as compose runs it: its image, entrypoint and environment from
    the env files, logging in over TCP to the host name ``postgres``."""
    import docker

    service = _db_bootstrap_service()
    env = envfiles.file_values(checkout, "env")
    environment = {
        **envfiles.file_values(checkout, "owner"),
        "POSTGRES_PASSWORD": superuser_password,
        "POSTGRES_USER": env["POSTGRES_USER"],
        "POSTGRES_DB": env["POSTGRES_DB"],
        "POSTGRES_WEB_PASSWORD": env["POSTGRES_WEB_PASSWORD"],
        "POSTGRES_ADMIN_PASSWORD": env["POSTGRES_ADMIN_PASSWORD"],
    }
    entrypoint = [part.replace("$$", "$") for part in service["entrypoint"]]
    container = docker.from_env().containers.run(
        service["image"],
        entrypoint=entrypoint,
        environment=environment,
        extra_hosts={"postgres": postgres_address},
        volumes={
            str(REPO_ROOT / "docker" / "postgres" / "bootstrap.sql"): {
                "bind": "/bootstrap.sql",
                "mode": "ro",
            }
        },
        detach=True,
    )
    try:
        exit_code = int(container.wait(timeout=120)["StatusCode"])
        return exit_code, container.logs().decode()
    finally:
        container.remove(force=True)


def test_db_bootstrap_logs_in_to_the_old_volume_with_the_carried_password(
    checkout: Path,
) -> None:
    """The volume accepts only the superuser password it was initialised with. The
    split carries it, where a regenerated one is refused."""
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _legacy(checkout)
    envfiles.split_legacy_env(checkout)

    with PostgresContainer(
        image=_db_bootstrap_service()["image"],
        username="openscientist",
        password=SUPERUSER_PASSWORD,
        dbname="openscientist",
    ) as postgres:
        wrapped = postgres.get_wrapped_container()
        wrapped.reload()
        address = wrapped.attrs["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]

        refused, output = _run_db_bootstrap(checkout, address, "freshly-generated-password")
        assert refused != 0
        assert "password authentication failed" in output

        carried = envfiles.file_values(checkout, "db")["POSTGRES_PASSWORD"]
        exit_code, output = _run_db_bootstrap(checkout, address, carried)
        assert exit_code == 0, output

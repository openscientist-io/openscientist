"""The compose file publishes the web on the bind address, loopback by default,
and the launcher environment holds only the keys its code reads."""

import re
from pathlib import Path

import yaml

from openscientist.cli import envfiles
from openscientist_launcher.config import LauncherConfig

_COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
# The web's single published port, written as bind-address:PORT:8080.
_PORT_SPEC = re.compile(r"^\$\{(?P<name>[^:}]+):-(?P<default>[^:}]+)\}:\$\{PORT:-8080\}:8080$")

# The interpolation forms the compose file uses, and the keys a holder of the
# Docker socket must never see in the launcher's environment.
_INTERPOLATION = re.compile(
    r"\$(\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>[:])(?P<rest>[^}]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def _is_secret_key(key: str) -> bool:
    if key == "OPENSCIENTIST_SECRET_KEY":
        return True
    if key.endswith(("_API_KEY", "_API_KEYS", "_CLIENT_SECRET", "_ACCESS_KEY")):
        return True
    if key.startswith("POSTGRES_") and key.endswith("_PASSWORD"):
        return True
    return key.startswith(("AWS_", "AZURE_", "GCP_"))


def _filled_env() -> dict[str, str]:
    """The project env a rendered deployment has: the example files, plus the
    values the wizard and `uv run openscientist` write into .env."""
    values: dict[str, str] = {}
    for name in envfiles.EXAMPLE_FILES.values():
        values.update(
            envfiles.parse_values((_COMPOSE.parent / name).read_text(encoding="utf-8").splitlines())
        )
    values["OPENSCIENTIST_HOST_PROJECT_DIR"] = "/srv/openscientist"
    values["AGENT_IMAGE_DIGESTS"] = "sha256:" + "a" * 64
    values["EXECUTOR_IMAGE_DIGESTS"] = "sha256:" + "b" * 64
    values["LAUNCHER_INSTANCE_ID"] = "test-instance"
    values["POSTGRES_WEB_PASSWORD"] = "w"
    values["POSTGRES_ADMIN_PASSWORD"] = "a"
    values["POSTGRES_PASSWORD"] = "s"
    values["POSTGRES_OWNER_PASSWORD"] = "o"
    return values


def _interpolate(value: str, values: dict[str, str]) -> str:
    def _one(match: re.Match[str]) -> str:
        if match.group("bare"):
            return values.get(match.group("bare"), "")
        name = match.group("name")
        rest = match.group("rest")
        if rest.startswith("?"):
            if not values.get(name):
                raise AssertionError(
                    f"the compose file requires {name}, which the env does not set"
                )
            return values[name]
        if rest.startswith("-"):
            return values.get(name) or rest[1:]
        return values.get(name, "")

    return _INTERPOLATION.sub(_one, value)


def _launcher_environment() -> dict[str, str]:
    """The launcher service's environment the way compose renders it: each
    env_file entry verbatim, then the explicit environment, which wins."""
    document = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    launcher = document["services"]["launcher"]
    values = _filled_env()
    env: dict[str, str] = {}
    for entry in launcher.get("env_file", []):
        name = entry["path"] if isinstance(entry, dict) else entry
        path = _COMPOSE.parent / name
        if not path.is_file():
            # The checkout ships the example the wizard starts from, and the
            # deployment fills it in, so model the file from its example.
            path = _COMPOSE.parent / f"{name}.example"
        if path.is_file():
            file_values = envfiles.parse_values(path.read_text(encoding="utf-8").splitlines())
            # The examples carry empty secrets, the wizard fills them, so the
            # filled value stands in for the deployment's file contents.
            env.update({key: values.get(key) or value for key, value in file_values.items()})
    for key, raw in (launcher.get("environment") or {}).items():
        env[key] = _interpolate(str(raw), values)
    return env


def test_the_web_port_is_published_on_the_loopback_bind_address_by_default():
    document = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    ports = document["services"]["openscientist"]["ports"]
    assert len(ports) == 1
    match = _PORT_SPEC.match(ports[0])
    assert match is not None
    assert match.group("name") == "OPENSCIENTIST_BIND_ADDRESS"
    assert match.group("default") == "127.0.0.1"


def test_the_web_container_sees_the_bind_address_it_is_published_on():
    document = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    environment = document["services"]["openscientist"]["environment"]
    assert environment["OPENSCIENTIST_BIND_ADDRESS"] == "${OPENSCIENTIST_BIND_ADDRESS:-127.0.0.1}"


def test_the_launcher_loads_no_env_file():
    document = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    assert "env_file" not in document["services"]["launcher"]


def test_the_launcher_environment_holds_no_secret():
    env = _launcher_environment()
    held = sorted(key for key in env if _is_secret_key(key))
    assert not held, f"the launcher environment holds secrets: {held}"


def test_the_launcher_environment_holds_every_key_its_config_reads():
    env = _launcher_environment()
    cfg = LauncherConfig.from_env(env)
    assert cfg.jobs_root == Path("/srv/openscientist/jobs")
    assert cfg.instance_id == "test-instance"

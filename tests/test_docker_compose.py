"""The compose file publishes the web on the bind address, loopback by default."""

import re
from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
# The web's single published port, written as bind-address:PORT:8080.
_PORT_SPEC = re.compile(r"^\$\{(?P<name>[^:}]+):-(?P<default>[^:}]+)\}:\$\{PORT:-8080\}:8080$")


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

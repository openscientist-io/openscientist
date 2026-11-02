"""The Docker memory strings seeding converts to whole MiB."""

from __future__ import annotations

import pytest

from openscientist.admin_settings import parse_docker_memory_mib


@pytest.mark.parametrize(
    ("value", "mib"),
    [
        ("8g", 8192),
        ("512m", 512),
        ("1024k", 1),
        ("1048576b", 1),
        ("1G", 1024),
        ("2GB", 2048),
        ("512M", 512),
        ("1024KB", 1),
        ("256mB", 256),
        ("1500m", 1500),
        ("4294967296", 4096),
    ],
)
def test_the_units_docker_accepts(value: str, mib: int) -> None:
    assert parse_docker_memory_mib(value, "OPENSCIENTIST_AGENT_MEMORY") == mib


@pytest.mark.parametrize(
    "value",
    [
        "8x",
        "8.5g",
        "g",
        "",
        "8 gb",
        "-8g",
        "+8g",
        "0x10",
        "1024",
        "0m",
        "511k",
        "1048575",
        "8g5",
        "8g ",
        " 8g",
        "1.5g",
        "8 g",
    ],
)
def test_a_malformed_string_is_rejected_naming_the_variable(value: str) -> None:
    with pytest.raises(ValueError, match="OPENSCIENTIST_AGENT_MEMORY"):
        parse_docker_memory_mib(value, "OPENSCIENTIST_AGENT_MEMORY")

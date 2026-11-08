"""Docker memory strings, as ``OPENSCIENTIST_AGENT_MEMORY`` and its siblings hold them."""

from __future__ import annotations

import re

_MIB = 1024**2
_MEMORY = re.compile(r"^(\d+)(?:([bkmBKMgG])([bB])?)?$")
_UNITS = {"b": 1, "k": 1024, "m": _MIB, "g": _MIB * 1024}


def parse_docker_memory_mib(value: str, variable: str) -> int:
    """The whole MiB a Docker memory string like ``8g``, ``512m`` or a byte count holds, truncated.

    The units are Docker's, with or without the trailing ``b`` and any case.

    Raises:
        ValueError: the string is not a Docker memory string or holds less than 1 MiB, named by ``variable``.
    """
    match = _MEMORY.match(value)
    if match is None:
        raise ValueError(
            f"{variable} must be a Docker memory string like 8g or 512m, got {value!r}"
        )
    mib = int(match[1]) * _UNITS[(match[2] or "b").lower()] // _MIB
    if mib < 1:
        raise ValueError(f"{variable} must be at least 1 MiB, got {value!r}")
    return mib

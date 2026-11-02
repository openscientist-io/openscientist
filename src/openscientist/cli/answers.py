"""The setup wizard's answers file: one TOML table per step, without the secrets.

The file takes every answer from its tables, keyed by the step's key then the
question's key (the step's prefix dropped). String, int and bool values are
accepted; a step's questions say which kinds they want. Every question must be
answered, except a question of kind secret, which is optional and falls back
to the value the checkout already holds. The provider step also takes a
``skip_live_check`` switch: set it true to skip the live check that would make
a billed request, and absent or false to run it.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

#: A scalar the answers file holds: the kinds the questions produce, plus bool.
Scalar = str | int | bool

#: The provider table's switch that skips the billed live check.
SKIP_LIVE_CHECK = "skip_live_check"

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class AnswersError(ValueError):
    """The answers file cannot be read, or one of its entries is not a scalar."""


def read_answers(path: Path) -> dict[str, dict[str, Scalar]]:
    """The file's tables: step key to question key to value.

    Raises AnswersError naming the problem when the file does not exist, is not
    valid TOML, or an entry is not a string, int or bool.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise AnswersError(f"No such answers file: {path}") from None
    except tomllib.TOMLDecodeError as err:
        raise AnswersError(f"Not a valid TOML answers file {path}: {err}") from None
    tables: dict[str, dict[str, Scalar]] = {}
    for step_key, table in data.items():
        if not isinstance(table, dict):
            raise AnswersError(f"The [{step_key}] entry of {path} must be a table of answers")
        for key, value in table.items():
            if not isinstance(value, (bool, str, int)):
                raise AnswersError(
                    f"Invalid answer: [{step_key}] {key} must be a string, a number or true/false"
                )
        tables[step_key] = dict(table)
    return tables


def write_answers(path: Path, tables: Mapping[str, Mapping[str, Scalar]]) -> None:
    """The tables as TOML, one section per step in the given order, empty steps dropped."""
    lines: list[str] = []
    for step_key, table in tables.items():
        if not table:
            continue
        lines.append(f"[{_key(step_key)}]")
        for key, value in table.items():
            lines.append(f"{_key(key)} = {_value(value)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def question_key(step_key: str, full_key: str) -> str:
    """The question's key inside its step's table: the step's prefix dropped."""
    prefix = f"{step_key}."
    return full_key[len(prefix) :] if full_key.startswith(prefix) else full_key


def is_true(value: Scalar | None) -> bool:
    """Whether a switch answer is on: bools and the truthy strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on")
    return False


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _string(key)


def _value(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _string(value)


def _string(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'

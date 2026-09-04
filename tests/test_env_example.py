"""Guardrail on .env.example so ``cp .env.example .env`` works verbatim."""

import re
from pathlib import Path

_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
_INLINE_COMMENT = re.compile(r"\s#")


def test_active_assignments_carry_no_inline_comment() -> None:
    """Docker's env-file parser keeps a trailing comment inside the value."""
    offenders: list[str] = []
    lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if _INLINE_COMMENT.search(value):
            offenders.append(f"line {number}: {name}")

    assert offenders == [], "Move the trailing comment above the assignment: " + ", ".join(
        offenders
    )

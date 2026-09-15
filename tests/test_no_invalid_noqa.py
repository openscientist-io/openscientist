"""Guardrails for lint directive hygiene."""

from pathlib import Path


def test_no_invalid_env_ok_noqa_directives() -> None:
    project_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []

    for py_file in (project_root / "src").rglob("*.py"):
        for line_no, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if "# noqa: env-ok" in line:
                offenders.append(f"{py_file.relative_to(project_root)}:{line_no}")

    assert offenders == []

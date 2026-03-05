"""Guardrails around runtime usage of filesystem config.json."""

from pathlib import Path

_ALLOWED_REFERENCES = {
    Path("src/openscientist/bootstrap.py"),  # migration-only read source
    Path("src/openscientist/artifact_packager.py"),  # explicit archive exclusion list
}


def test_config_json_references_are_limited_to_migration_paths() -> None:
    """config.json must not become a runtime source outside migration code."""
    repo_root = Path(__file__).resolve().parents[1]
    source_root = repo_root / "src" / "openscientist"
    offenders: list[Path] = []

    for file_path in source_root.rglob("*.py"):
        rel_path = file_path.relative_to(repo_root)
        if rel_path in _ALLOWED_REFERENCES:
            continue
        if "config.json" in file_path.read_text(encoding="utf-8"):
            offenders.append(rel_path)

    assert offenders == [], (
        "Unexpected config.json references outside migration scope: "
        + ", ".join(str(path) for path in offenders)
    )

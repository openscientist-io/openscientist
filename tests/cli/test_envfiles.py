"""Env file handling: comments kept, keys replaced and appended, mode 0600, secrets once."""

import os
import re
import stat
from pathlib import Path

import pytest

from openscientist.cli import envfiles


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_fresh_setup_keeps_comments_replaces_and_appends_keys(checkout: Path):
    envfiles.write_env_files(
        checkout,
        {
            "env": {
                "OPENSCIENTIST_PROVIDER": "vllm",
                "OPENSCIENTIST_BASE_URL": "http://example.org:9000",
                "SOME_NEW_KEY": "appended",
            }
        },
    )
    env = (checkout / ".env").read_text(encoding="utf-8")
    assert "OPENSCIENTIST_PROVIDER=vllm" in env
    # The commented example line is un-commented with its trailing comment kept.
    base_url_line = next(
        line for line in env.splitlines() if line.startswith("OPENSCIENTIST_BASE_URL=")
    )
    assert base_url_line.startswith("OPENSCIENTIST_BASE_URL=http://example.org:9000")
    assert base_url_line.endswith("# Base URL for notifications and share links")
    # Unanswered keys keep their example state.
    assert "POSTGRES_PORT=5434" in env
    assert "# Which LLM backend to use." in env
    # A key no question touched keeps its example value.
    assert "ANTHROPIC_API_KEY=sk-ant-api03-your-key-here" in env
    assert env.endswith("SOME_NEW_KEY=appended\n")
    for name in (".env", ".env.db", ".env.owner"):
        assert _mode(checkout / name) == 0o600


def test_second_setup_keeps_generated_secrets_byte_identical(checkout: Path):
    envfiles.write_env_files(checkout, {})
    first = {name: (checkout / name).read_bytes() for name in (".env", ".env.db", ".env.owner")}
    for key in envfiles.GENERATED_SECRETS:
        value = envfiles.file_values(checkout, envfiles.GENERATED_SECRETS[key]).get(key, "")
        assert value, key
    assert re.fullmatch(
        r"[0-9a-f]{64}", envfiles.file_values(checkout, "env")["OPENSCIENTIST_SECRET_KEY"]
    )
    envfiles.write_env_files(checkout, {})
    for name, content in first.items():
        assert (checkout / name).read_bytes() == content, name


def test_existing_secret_is_never_replaced(checkout: Path):
    envfiles.write_env_files(checkout, {})
    db_lines = (checkout / ".env.db").read_text(encoding="utf-8").splitlines()
    db_lines = [
        line if not line.startswith("POSTGRES_PASSWORD=") else "POSTGRES_PASSWORD=kept-value"
        for line in db_lines
    ]
    envfiles.write_file(checkout / ".env.db", db_lines)
    envfiles.write_env_files(checkout, {})
    assert envfiles.file_values(checkout, "db")["POSTGRES_PASSWORD"] == "kept-value"


def test_a_secret_left_at_its_example_placeholder_is_generated(checkout: Path):
    """An env file copied from the example by hand never keeps the example's placeholder as a secret."""
    (checkout / ".env").write_text((checkout / ".env.example").read_text(encoding="utf-8"))
    (checkout / ".env").write_text(
        (checkout / ".env").read_text(encoding="utf-8") + "OPENSCIENTIST_PROVIDER=anthropic\n"
    )
    for name in (".env.db", ".env.owner"):
        example = (checkout / f"{name}.example").read_text(encoding="utf-8")
        (checkout / name).write_text(example.replace("PASSWORD=\n", "PASSWORD=set-by-hand\n"))
    (checkout / ".env").write_text(
        (checkout / ".env")
        .read_text(encoding="utf-8")
        .replace("POSTGRES_WEB_PASSWORD=\n", "POSTGRES_WEB_PASSWORD=set-by-hand\n")
        .replace("POSTGRES_ADMIN_PASSWORD=\n", "POSTGRES_ADMIN_PASSWORD=set-by-hand\n")
    )
    assert not envfiles.is_configured(checkout)

    envfiles.write_env_files(checkout, {})

    secret = envfiles.file_values(checkout, "env")["OPENSCIENTIST_SECRET_KEY"]
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert envfiles.file_values(checkout, "db")["POSTGRES_PASSWORD"] == "set-by-hand"
    assert envfiles.is_configured(checkout)


def test_is_configured(checkout: Path):
    assert not envfiles.is_configured(checkout)
    envfiles.write_env_files(checkout, {"env": {"OPENSCIENTIST_PROVIDER": "anthropic"}})
    assert envfiles.is_configured(checkout)
    (checkout / ".env.db").unlink()
    assert not envfiles.is_configured(checkout)


def test_set_value_replaces_uncommented_line_in_place():
    lines = ["A=1", "# A=2", "B=3"]
    assert envfiles.set_value(lines, "A", "9") == ["A=9", "# A=2", "B=3"]


def test_set_value_appends_missing_key():
    assert envfiles.set_value(["A=1"], "C", "2") == ["A=1", "C=2"]


def test_parse_values_drops_comments_and_trailing_comments():
    lines = ["# KEY=0", "KEY=hello # note", "EMPTY=", "OTHER=  spaced  ", "NOVALUE"]
    assert envfiles.parse_values(lines) == {"KEY": "hello", "OTHER": "spaced"}


def test_write_file_removes_the_temp_file_when_the_replace_fails(checkout: Path, monkeypatch):
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    before = set(checkout.iterdir())
    with pytest.raises(OSError):
        envfiles.write_file(checkout / ".env", ["A=1"])
    assert not (checkout / ".env").exists()
    assert set(checkout.iterdir()) == before


def test_write_file_still_raises_when_the_cleanup_unlink_fails(checkout: Path, monkeypatch):
    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("replace failed")

    def unlink_fail(*args: object, **kwargs: object) -> None:
        raise OSError("unlink failed")

    monkeypatch.setattr(os, "replace", fail)
    monkeypatch.setattr(os, "unlink", unlink_fail)
    with pytest.raises(PermissionError):
        envfiles.write_file(checkout / ".env", ["A=1"])


def test_example_value_is_empty_when_the_example_file_is_missing(tmp_path: Path):
    assert envfiles._example_value(tmp_path, "env", "POSTGRES_PASSWORD") == ""

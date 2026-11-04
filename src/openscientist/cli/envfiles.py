"""The three env files the wizard reads and writes."""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

#: The wizard's file names and the examples they start from.
ENV_FILES = {"env": ".env", "db": ".env.db", "owner": ".env.owner"}
EXAMPLE_FILES = {"env": ".env.example", "db": ".env.db.example", "owner": ".env.owner.example"}

#: Secrets the wizard generates once and never replaces again.
GENERATED_SECRETS = {
    "POSTGRES_WEB_PASSWORD": "env",
    "POSTGRES_ADMIN_PASSWORD": "env",
    "OPENSCIENTIST_SECRET_KEY": "env",
    "LAUNCHER_INSTANCE_ID": "owner",
    "POSTGRES_PASSWORD": "db",
    "POSTGRES_OWNER_PASSWORD": "owner",
}

# A value, then an optional whitespace-prefixed trailing comment.
_TRAILING_COMMENT = re.compile(r"^(.*?)(\s+#.*)?$")


def _file_path(root: Path, file: str) -> Path:
    return root / ENV_FILES[file]


def _baseline_lines(root: Path, file: str) -> list[str]:
    """The file's lines, starting from the example on a fresh checkout."""
    path = _file_path(root, file)
    source = path if path.is_file() else root / EXAMPLE_FILES[file]
    return source.read_text(encoding="utf-8").splitlines()


def _is_active(line: str, key: str) -> bool:
    """An uncommented KEY= line."""
    return re.match(rf"^{re.escape(key)}=", line) is not None


def _is_commented(line: str, key: str) -> bool:
    """A commented-out # KEY= line."""
    return re.match(rf"^\s*#\s*{re.escape(key)}=", line) is not None


def _rewritten_line(line: str, key: str, value: str) -> str:
    """The line rewritten for ``key``, keeping any trailing comment."""
    body = line.lstrip("#").lstrip()
    rest = body.split("=", 1)[1]
    match = _TRAILING_COMMENT.match(rest)
    trailing = match.group(2) if match and match.group(2) else ""
    return f"{key}={value}{trailing}"


#: The .env key holding the checkout's absolute host path.
HOST_PROJECT_DIR = "OPENSCIENTIST_HOST_PROJECT_DIR"


def set_value(lines: list[str], key: str, value: str) -> list[str]:
    """Replace ``key``'s line, un-commenting it, or append it when absent."""
    replaced = False
    updated: list[str] = []
    for line in lines:
        if not replaced and (_is_active(line, key) or _is_commented(line, key)):
            updated.append(_rewritten_line(line, key, value))
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"{key}={value}")
    return updated


def set_file_value(root: Path, file: str, key: str, value: str) -> None:
    """Rewrite one key of one env file on top of the baseline, atomically."""
    lines = set_value(_baseline_lines(root, file), key, value)
    write_file(_file_path(root, file), lines)


def record_host_project_dir(root: Path) -> None:
    """Record the checkout's absolute path in .env. Compose mounts the launcher's
    jobs directory from it at the same path, since Docker resolves bind sources
    on the host."""
    path = str(root.resolve())
    if file_values(root, "env").get(HOST_PROJECT_DIR) != path:
        set_file_value(root, "env", HOST_PROJECT_DIR, path)


def parse_values(lines: Iterable[str]) -> dict[str, str]:
    """Uncommented KEY=VALUE pairs, last one wins, with trailing comments dropped."""
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        match = _TRAILING_COMMENT.match(rest)
        value = (match.group(1) if match else rest).strip()
        if key.strip() and value:
            values[key.strip()] = value
    return values


def write_file(path: Path, lines: list[str]) -> None:
    """Write the file atomically with owner-only permissions."""
    text = "\n".join(lines).rstrip() + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _generate_secret(key: str) -> str:
    if key == "OPENSCIENTIST_SECRET_KEY":
        return secrets.token_hex(32)
    if key == "LAUNCHER_INSTANCE_ID":
        # A label value, not a credential: short and readable.
        return secrets.token_hex(6)
    return secrets.token_urlsafe(32)


def file_values(root: Path, file: str) -> dict[str, str]:
    """The current values of one file, from the example when it does not exist yet."""
    return parse_values(_baseline_lines(root, file))


def current_values(root: Path) -> dict[str, str]:
    """The values of the env files that exist, db and owner first so the web's file wins on a clash."""
    values: dict[str, str] = {}
    for file in ("db", "owner", "env"):
        path = _file_path(root, file)
        if path.is_file():
            values.update(parse_values(path.read_text(encoding="utf-8").splitlines()))
    return values


def _example_value(root: Path, file: str, key: str) -> str:
    example = root / EXAMPLE_FILES[file]
    if not example.is_file():
        return ""
    return parse_values(example.read_text(encoding="utf-8").splitlines()).get(key, "")


def _is_real_secret(root: Path, file: str, key: str, value: str) -> bool:
    """Set, and not the example's placeholder, which a hand-copied file still carries."""
    return bool(value) and value != _example_value(root, file, key)


def write_env_files(root: Path, answers: Mapping[str, Mapping[str, str]]) -> None:
    """Write the three env files, generating the missing secrets once."""
    for file in ("env", "db", "owner"):
        existed = _file_path(root, file).is_file()
        values = dict(answers.get(file, {}))
        for key, target in GENERATED_SECRETS.items():
            if target != file:
                continue
            current = file_values(root, file).get(key, "")
            if existed and _is_real_secret(root, file, key, current):
                values[key] = current
            else:
                values[key] = _generate_secret(key)
        write_file(_file_path(root, file), build_content(root, file, values))


def build_content(root: Path, file: str, values: Mapping[str, str]) -> list[str]:
    """The file's lines after applying ``values`` on top of the baseline."""
    lines = _baseline_lines(root, file)
    for key, value in values.items():
        lines = set_value(lines, key, value)
    return lines


def is_configured(root: Path) -> bool:
    """A checkout is configured when the three files hold the five secrets and a provider."""
    for file in ENV_FILES:
        if not _file_path(root, file).is_file():
            return False
    for file in ENV_FILES:
        present = parse_values(_file_path(root, file).read_text(encoding="utf-8").splitlines())
        for key, target in GENERATED_SECRETS.items():
            if target == file and not _is_real_secret(root, file, key, present.get(key, "")):
                return False
    values: dict[str, str] = {}
    for file in ENV_FILES:
        values.update(parse_values(_file_path(root, file).read_text(encoding="utf-8").splitlines()))
    return bool(values.get("OPENSCIENTIST_PROVIDER"))

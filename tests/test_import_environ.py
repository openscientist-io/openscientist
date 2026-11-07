"""Importing the app leaves the process environment alone.

The CLI hands its environment to docker compose, which prefers it over .env
when it fills the compose file, so a .env value copied in at import shadows
every value the CLI writes to .env later in the same run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SENTINEL = "OPENSCIENTIST_IMPORT_ENVIRON_SENTINEL"

# Imports the module named by argv[1], runs the statement in argv[2] with the
# module bound to ``m``, then prints the names of the variables that changed.
_PROBE = """\
import importlib, json, os, sys
before = dict(os.environ)
m = importlib.import_module(sys.argv[1])
exec(sys.argv[2])
after = dict(os.environ)
print(json.dumps(sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))))
"""


@pytest.mark.parametrize(
    ("module", "then"),
    [
        ("openscientist.cli.app", "pass"),
        # The memory check runs before the builds and used to import its parser lazily.
        ("openscientist.cli.preflight", "m._memory({'MemTotal': 1}, {})"),
        ("openscientist.admin_settings", "pass"),
        ("openscientist.database", "pass"),
        ("openscientist.web_app", "pass"),
    ],
)
def test_importing_a_module_changes_no_environment_variable(
    module: str, then: str, tmp_path: Path
) -> None:
    """A fresh interpreter in a directory with a .env imports the module and
    reports every variable whose value changed."""
    (tmp_path / ".env").write_text(
        f"AGENT_IMAGE_DIGESTS=sha256:stale\n{_SENTINEL}=from-the-env-file\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_IMAGE_DIGESTS", _SENTINEL)}
    result = subprocess.run(
        (sys.executable, "-c", _PROBE, module, then),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []

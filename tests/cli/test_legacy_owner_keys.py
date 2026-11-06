"""An existing checkout upgrades its env files without a manual step."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles, stack
from tests.cli.conftest import env_value, wizard_tail

runner = CliRunner()

OLD_INSTANCE_ID = "legacyinstance"
AGENT_TAG = "openscientist-agent:latest"
AGENT_DIGEST = "sha256:" + "a" * 64
EXECUTOR_DIGEST = "sha256:" + "b" * 64

LAUNCHER_KEYS = ("LAUNCHER_INSTANCE_ID", "AGENT_IMAGE_DIGESTS", "EXECUTOR_IMAGE_DIGESTS")


def _old_layout(checkout: Path) -> None:
    """A configured checkout made before the launcher keys moved to .env: the
    marker label and both digest sets live in .env.owner, .env lacks them."""
    (checkout / ".env").write_text(
        (checkout / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for key, value in {
        "POSTGRES_WEB_PASSWORD": "w",
        "POSTGRES_ADMIN_PASSWORD": "a",
        "OPENSCIENTIST_SECRET_KEY": "c" * 64,
    }.items():
        envfiles.set_file_value(checkout, "env", key, value)
    envfiles.set_file_value(checkout, "db", "POSTGRES_PASSWORD", "s")
    envfiles.set_file_value(checkout, "owner", "POSTGRES_OWNER_PASSWORD", "o")
    envfiles.set_file_value(checkout, "owner", "LAUNCHER_INSTANCE_ID", OLD_INSTANCE_ID)
    envfiles.set_file_value(checkout, "owner", "AGENT_IMAGE_DIGESTS", AGENT_DIGEST)
    envfiles.set_file_value(checkout, "owner", "EXECUTOR_IMAGE_DIGESTS", EXECUTOR_DIGEST)


def test_start_moves_the_launcher_keys_out_of_the_owner_file(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv run openscientist` on an old checkout starts straight to the stack,
    with the old marker label and digest sets now in .env."""
    _old_layout(checkout)
    assert not envfiles.is_configured(checkout)

    def run(argv: tuple[str, ...], env: Mapping[str, str] | None) -> int:
        return 0

    def _image_id(image: str) -> str:
        return AGENT_DIGEST if image == AGENT_TAG else EXECUTOR_DIGEST

    monkeypatch.setattr(stack, "run_command", run)
    monkeypatch.setattr(stack, "_image_id", _image_id)
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)

    result = runner.invoke(cli_app.app, [])
    assert result.exit_code == 0, result.output
    assert "Not configured yet" not in result.output

    env = envfiles.file_values(checkout, "env")
    assert env["LAUNCHER_INSTANCE_ID"] == OLD_INSTANCE_ID
    assert env["AGENT_IMAGE_DIGESTS"] == AGENT_DIGEST
    assert env["EXECUTOR_IMAGE_DIGESTS"] == EXECUTOR_DIGEST
    owner = envfiles.file_values(checkout, "owner")
    for key in LAUNCHER_KEYS:
        assert key not in owner
    assert envfiles.is_configured(checkout)


def test_the_wizard_keeps_the_instance_id_of_an_existing_checkout(checkout: Path) -> None:
    """Re-running the setup never mints a second marker label: a running stack's
    containers keep answering the launcher."""
    _old_layout(checkout)
    # The provider is already set, so the wizard skips its choice question and
    # asks the provider's own questions straight away.
    setup_input = "\n".join(["sk-test-123", "", ""] + wizard_tail()) + "\n"
    result = runner.invoke(cli_app.app, ["setup"], input=setup_input)
    assert result.exit_code == 0, result.output
    assert env_value(checkout, ".env", "LAUNCHER_INSTANCE_ID") == OLD_INSTANCE_ID

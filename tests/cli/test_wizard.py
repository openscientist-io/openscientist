"""The step flow driven by Typer's CliRunner on a temp checkout."""

import os
import stat
from pathlib import Path

from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles, stack
from tests.cli.conftest import ANTHROPIC_SETUP_ANSWERS, env_value

runner = CliRunner()


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_setup_writes_env_files(checkout: Path):
    result = runner.invoke(cli_app.app, ["setup"], input=ANTHROPIC_SETUP_ANSWERS)
    assert result.exit_code == 0
    for name in (".env", ".env.db", ".env.owner"):
        assert (checkout / name).is_file()
        assert _mode(checkout / name) == 0o600
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "anthropic"
    assert env_value(checkout, ".env", "ANTHROPIC_API_KEY") == "sk-test-123"
    assert env_value(checkout, ".env", "PORT") == "8080"
    assert env_value(checkout, ".env", "POSTGRES_PORT") == "5434"
    # The secrets are generated but never shown.
    for key in envfiles.GENERATED_SECRETS:
        value = envfiles.file_values(checkout, envfiles.GENERATED_SECRETS[key]).get(key, "")
        assert value, key
        assert value not in result.output
    # The example's comments survive the write.
    assert "# OpenScientist Environment Configuration" in (checkout / ".env").read_text(
        encoding="utf-8"
    )


def test_provider_step_asks_only_the_chosen_provider_questions(checkout: Path):
    # Provider 9 is vLLM: its endpoint and model, then every later question at its default.
    result = runner.invoke(
        cli_app.app,
        ["setup"],
        input="9\nhttp://host.docker.internal:8000/v1\n\nqwen3-8b\n\n\n\n\n\n",
    )
    assert result.exit_code == 0
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "vllm"
    assert env_value(checkout, ".env", "VLLM_BASE_URL") == "http://host.docker.internal:8000/v1"
    assert env_value(checkout, ".env", "OPENSCIENTIST_MODEL") == "qwen3-8b"
    # The Anthropic questions were never asked.
    assert "ANTHROPIC_API_KEY" not in result.output
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in result.output


def test_setup_rejects_answers_the_provider_validation_refuses(checkout: Path):
    # Round one answers the key questions empty, so the provider refuses; the
    # wizard re-asks the step and the round-two key is written.
    result = runner.invoke(
        cli_app.app,
        ["setup"],
        input="1\n\n\n\nsk-test-123\n\n\n\n\n\n\n\n",
    )
    assert result.exit_code == 0
    assert "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN not set" in result.output
    assert "trying again" in result.output
    assert env_value(checkout, ".env", "ANTHROPIC_API_KEY") == "sk-test-123"


def test_second_setup_offers_current_values_and_keeps_secrets(checkout: Path):
    first = runner.invoke(cli_app.app, ["setup"], input=ANTHROPIC_SETUP_ANSWERS)
    assert first.exit_code == 0
    before = {name: (checkout / name).read_bytes() for name in (".env", ".env.db", ".env.owner")}
    second = runner.invoke(cli_app.app, ["setup"], input="\n" * 9)
    assert second.exit_code == 0
    for name, content in before.items():
        assert (checkout / name).read_bytes() == content, name


def test_default_command_starts_without_asking_when_configured(checkout: Path, monkeypatch):
    assert runner.invoke(cli_app.app, ["setup"], input=ANTHROPIC_SETUP_ANSWERS).exit_code == 0
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(stack, "run_command", fake_run)
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    result = runner.invoke(cli_app.app, [], input="")
    assert result.exit_code == 0
    assert "OPENSCIENTIST_PROVIDER" not in result.output
    assert [argv for argv, _ in calls][-1] == (
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "up",
        "-d",
        "--remove-orphans",
        "openscientist",
    )


def test_default_command_runs_wizard_then_starts(checkout: Path, monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None) -> int:
        calls.append((argv, env))
        return 0

    monkeypatch.setattr(stack, "run_command", fake_run)
    monkeypatch.setattr(stack, "wait_for_health", lambda url, timeout=300.0, interval=3.0: True)
    result = runner.invoke(cli_app.app, [], input=ANTHROPIC_SETUP_ANSWERS)
    assert result.exit_code == 0
    assert "Not configured yet" in result.output
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "anthropic"
    assert len(calls) == 5
    assert calls[-1][0][-1] == "openscientist"

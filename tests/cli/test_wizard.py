"""The step flow driven by Typer's CliRunner on a temp checkout."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import typer
from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles, stack, wizard
from openscientist.cli.step import Question
from openscientist.cli.steps import STEPS
from tests.cli.conftest import ANTHROPIC_SETUP_ANSWERS, env_value, wizard_input, wizard_tail

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
    assert env_value(checkout, ".env", "OPENSCIENTIST_BIND_ADDRESS") == "127.0.0.1"
    assert env_value(checkout, ".env", "POSTGRES_PORT") == "5434"
    # The PR 3 steps land their answers.
    assert env_value(checkout, ".env", "OPENSCIENTIST_DEV_MODE") == "true"
    assert env_value(checkout, ".env", "OPENSCIENTIST_AIRGAPPED") == "False"
    assert env_value(checkout, ".env", "PHENIX_HOST_PATH") == ""
    # The secrets are generated but never shown.
    for key in envfiles.GENERATED_SECRETS:
        value = envfiles.file_values(checkout, envfiles.GENERATED_SECRETS[key]).get(key, "")
        assert value, key
        assert value not in result.output
    # The example's comments survive the write.
    assert "# OpenScientist Environment Configuration" in (checkout / ".env").read_text(
        encoding="utf-8"
    )


def test_setup_writes_the_bind_address(checkout: Path):
    lines = ["1", "sk-test-123", "", "", *wizard_tail(bind_address="10.0.0.5")]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert env_value(checkout, ".env", "OPENSCIENTIST_BIND_ADDRESS") == "10.0.0.5"


def test_provider_step_asks_only_the_chosen_provider_questions(checkout: Path):
    # Provider 9 is vLLM: its endpoint and model, then every later question at its default.
    result = runner.invoke(
        cli_app.app,
        ["setup"],
        input=wizard_input("9", ["http://host.docker.internal:8000/v1", "", "qwen3-8b"]),
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
    # wizard re-asks the step (not the choice) and the round-two key is written.
    input_lines = ["1", "", "", "", "sk-test-123", "", "", *wizard_tail()]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(input_lines) + "\n")
    assert result.exit_code == 0
    # The rejected first pass was re-asked (the provider choice was not). The
    # second pass's key is what got saved, and the run consumed both passes
    # through to the tail.
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "anthropic"
    assert env_value(checkout, ".env", "ANTHROPIC_API_KEY") == "sk-test-123"


def test_second_setup_offers_current_values_and_keeps_secrets(checkout: Path):
    first = runner.invoke(cli_app.app, ["setup"], input=ANTHROPIC_SETUP_ANSWERS)
    assert first.exit_code == 0
    before = {name: (checkout / name).read_bytes() for name in (".env", ".env.db", ".env.owner")}
    # A re-run skips the provider choice (it is already set) and offers the current
    # values as defaults, so every prompt is answered with Enter.
    second = runner.invoke(cli_app.app, ["setup"], input="\n" * 19)
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
    # The checkout starts unconfigured, so the env file's values prove the
    # wizard ran first. The four builds plus the compose up prove the start
    # then ran.
    assert (checkout / ".env").is_file()
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "anthropic"
    assert len(calls) == 5
    assert calls[-1][0][-1] == "openscientist"


def test_stop_command_propagates_the_compose_exit_code(checkout: Path, monkeypatch):
    monkeypatch.setattr(stack, "run_command", lambda argv, env: 4)
    assert runner.invoke(cli_app.app, ["stop"]).exit_code == 4
    monkeypatch.setattr(stack, "run_command", lambda argv, env: 0)
    assert runner.invoke(cli_app.app, ["stop"]).exit_code == 0


def test_default_command_propagates_the_start_failure(checkout: Path, monkeypatch):
    assert runner.invoke(cli_app.app, ["setup"], input=ANTHROPIC_SETUP_ANSWERS).exit_code == 0
    monkeypatch.setattr(stack, "run_command", lambda argv, env: 5)
    assert runner.invoke(cli_app.app, []).exit_code == 5


def test_module_entry_point_prints_the_help():
    result = subprocess.run(
        [sys.executable, "-m", "openscientist.cli", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "setup" in result.stdout


def test_setup_skips_steps_that_do_not_apply(checkout: Path, monkeypatch):
    harness = next(step for step in STEPS if step.key == "harness")

    def not_asked(answers: dict[str, str]) -> list[Question]:
        raise AssertionError("the harness step was asked although it does not apply")

    monkeypatch.setattr(harness, "applies", lambda answers: False)
    monkeypatch.setattr(harness, "questions", not_asked)
    tail = wizard_tail()
    del tail[3]  # the harness answer, never asked
    answers = "\n".join(["1", "sk-test-123", "", "", *tail]) + "\n"
    result = runner.invoke(cli_app.app, ["setup"], input=answers)
    assert result.exit_code == 0


def test_ask_without_a_default_reads_a_plain_answer():
    question = Question(key="x.port", env="SOME_PORT", file="env", kind="port")
    captured: dict[str, str] = {}
    cli = typer.Typer()

    @cli.command()
    def ask_port() -> None:
        captured["value"] = wizard.ask(question, None)

    result = runner.invoke(cli, input="42\n")
    assert result.exit_code == 0
    assert captured["value"] == "42"


def test_ask_reprompts_a_required_question_until_it_has_a_value(monkeypatch):
    import click

    question = Question(
        key="x.required", env="SOME_REQUIRED", file="env", kind="text", required=True
    )
    captured: dict[str, str] = {}
    cli = typer.Typer()
    answers = iter(("", "value"))
    prompts: list[str] = []

    def prompt(text, **kwargs):
        answer = next(answers)
        prompts.append(answer)
        return answer

    @cli.command()
    def ask_required() -> None:
        captured["value"] = wizard.ask(question, None)

    monkeypatch.setattr(click, "prompt", prompt)
    result = runner.invoke(cli)
    assert result.exit_code == 0
    # The empty answer was refused and the question asked again.
    assert prompts == ["", "value"]
    assert captured["value"] == "value"

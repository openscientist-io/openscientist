"""The answers file: setup from TOML, the round-trip, and the missing or invalid answers."""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from typer.testing import CliRunner

from openscientist.cli import answers as answers_io
from openscientist.cli import app as cli_app
from openscientist.cli import livecheck
from openscientist.providers.base import LiveCheckResult
from tests.cli.conftest import ANTHROPIC_SETUP_ANSWERS, env_value, wizard_tail

runner = CliRunner()

ENV_FILE_NAMES = (".env", ".env.db", ".env.owner")
EXAMPLE_NAMES = (".env.example", ".env.db.example", ".env.owner.example")


def _second_checkout(source: Path, monkeypatch) -> Path:
    """A fresh checkout next to ``source``, with its example and env files."""
    target = source / "second"
    target.mkdir()
    for name in EXAMPLE_NAMES:
        (target / name).write_text((source / name).read_text(encoding="utf-8"))
    for name in ENV_FILE_NAMES:
        (target / name).write_bytes((source / name).read_bytes())
    monkeypatch.chdir(target)
    return target


def _vllm_tables() -> dict[str, dict[str, str | int | bool]]:
    """A complete answers set for a host vLLM server with the dev mock login."""
    return {
        "provider": {
            "id": "vllm",
            "vllm_base_url": "http://host.docker.internal:43524/v1",
            "vllm_api_key": "",
            "openscientist_model": "qwen3.8-27b-nvfp4",
        },
        "address": {
            "base_url": "",
            "port": 8080,
            "bind_address": "127.0.0.1",
            "postgres_port": 5434,
        },
        "harness": {"id": "auto"},
        "concurrency": {"jobs": 4},
        "login": {
            "google_client_id": "",
            "google_client_secret": "",
            "github_client_id": "",
            "github_client_secret": "",
            "orcid_client_id": "",
            "orcid_client_secret": "",
            "dev_mode": "true",
            "bootstrap_admin_emails": "",
        },
        "airgap": {"enabled": ""},
        "phenix": {"host_path": ""},
    }


def _foundry_tables() -> dict[str, dict[str, str | int | bool]]:
    """A complete answers set for Azure Foundry, whose live check bills."""
    tables = _vllm_tables()
    tables["provider"] = {
        "id": "foundry",
        "anthropic_foundry_resource": "res",
        "anthropic_foundry_base_url": "",
        "anthropic_foundry_api_key": "key",
        "openscientist_model": "",
    }
    return tables


def _write(
    checkout: Path, tables: Mapping[str, Mapping[str, str | int | bool]], name: str = "answers.toml"
) -> Path:
    path = checkout / name
    answers_io.write_answers(path, tables)
    return path


def _no_env_files(checkout: Path) -> None:
    for name in ENV_FILE_NAMES:
        assert not (checkout / name).is_file(), name


# --- The answers file sets up the checkout -----------------------------------


def test_a_complete_file_sets_up_without_prompting_the_same_files_interactive_writes(
    checkout: Path, monkeypatch
):
    first = runner.invoke(
        cli_app.app,
        ["setup", "--write-answers", str(checkout / "answers.toml")],
        input=ANTHROPIC_SETUP_ANSWERS,
    )
    assert first.exit_code == 0
    before = {name: (checkout / name).read_bytes() for name in ENV_FILE_NAMES}
    second_checkout = _second_checkout(checkout, monkeypatch)
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(checkout / "answers.toml")], input=""
    )
    assert result.exit_code == 0
    # The replay asked nothing and kept the secrets, so the files are identical,
    # each recording its own checkout's path.
    for name in ENV_FILE_NAMES:
        replayed = (second_checkout / name).read_bytes()
        expected = before[name].replace(
            str(checkout.resolve()).encode(), str(second_checkout.resolve()).encode()
        )
        assert replayed == expected, name
    assert env_value(second_checkout, ".env", "OPENSCIENTIST_HOST_PROJECT_DIR") == str(
        second_checkout.resolve()
    )
    assert "required" not in result.output
    assert env_value(second_checkout, ".env", "OPENSCIENTIST_PROVIDER") == "anthropic"


def test_a_vllm_file_sets_up_a_fresh_checkout(checkout: Path):
    path = _write(checkout, _vllm_tables())
    result = runner.invoke(cli_app.app, ["setup", "--answers", str(path)], input="")
    assert result.exit_code == 0, result.output
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "vllm"
    assert env_value(checkout, ".env", "VLLM_BASE_URL") == "http://host.docker.internal:43524/v1"
    assert env_value(checkout, ".env", "OPENSCIENTIST_MODEL") == "qwen3.8-27b-nvfp4"
    assert env_value(checkout, ".env", "OPENSCIENTIST_DEV_MODE") == "true"
    assert env_value(checkout, ".env", "PORT") == "8080"


def test_a_chatgpt_login_sets_up_with_the_airgap_step(checkout: Path):
    tables = _vllm_tables()
    tables["provider"] = {
        "id": "openai",
        "codex_auth_host_path": str(checkout / "codex"),
        "openscientist_model": "gpt-5.2-codex",
    }
    tables["airgap"] = {"enabled": "false"}
    path = checkout / "answers.toml"
    result = runner.invoke(
        cli_app.app,
        [
            "setup",
            "--answers",
            str(_write(checkout, tables)),
            "--write-answers",
            str(path),
        ],
        input="",
    )
    assert result.exit_code == 0, result.output
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "openai"
    assert env_value(checkout, ".env", "OPENSCIENTIST_AIRGAPPED") == "false"
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["airgap"]["enabled"] == "false"
    assert written["provider"]["id"] == "openai"


def test_a_missing_optional_secret_keeps_the_current_value_and_succeeds(checkout: Path):
    tables = _vllm_tables()
    del tables["provider"]["vllm_api_key"]
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code == 0, result.output
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "vllm"
    assert env_value(checkout, ".env", "VLLM_API_KEY") == ""


def test_a_missing_answers_file_fails_and_writes_nothing(checkout: Path):
    result = runner.invoke(cli_app.app, ["setup", "--answers", str(checkout / "absent.toml")])
    assert result.exit_code != 0
    _no_env_files(checkout)


def test_invalid_toml_fails_and_writes_nothing(checkout: Path):
    path = checkout / "answers.toml"
    path.write_text("not a [valid toml = 3\n", encoding="utf-8")
    result = runner.invoke(cli_app.app, ["setup", "--answers", str(path)])
    assert result.exit_code != 0
    _no_env_files(checkout)


def test_a_table_entry_that_is_not_a_table_fails_and_writes_nothing(checkout: Path):
    path = checkout / "answers.toml"
    path.write_text("[provider]\nid = 'vllm'\n[address] = 3\n", encoding="utf-8")
    result = runner.invoke(cli_app.app, ["setup", "--answers", str(path)])
    assert result.exit_code != 0
    _no_env_files(checkout)


def test_a_top_level_value_that_is_not_a_table_fails_and_writes_nothing(checkout: Path):
    path = checkout / "answers.toml"
    path.write_text('provider = "vllm"\n', encoding="utf-8")
    result = runner.invoke(cli_app.app, ["setup", "--answers", str(path)])
    assert result.exit_code != 0
    _no_env_files(checkout)


def test_an_answer_that_is_not_a_scalar_fails_and_writes_nothing(checkout: Path):
    path = checkout / "answers.toml"
    path.write_text("[address]\nport = [8080]\n", encoding="utf-8")
    result = runner.invoke(cli_app.app, ["setup", "--answers", str(path)])
    assert result.exit_code != 0
    _no_env_files(checkout)


# --- Missing and invalid answers ----------------------------------------------


def test_missing_answers_fail_naming_every_missing_one_and_write_nothing(checkout: Path):
    tables = _vllm_tables()
    del tables["provider"]["vllm_base_url"]
    del tables["provider"]["openscientist_model"]
    del tables["airgap"]
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code != 0
    for key in ("provider.vllm_base_url", "provider.openscientist_model", "airgap.enabled"):
        assert key in result.output, key
    _no_env_files(checkout)


def test_an_invalid_port_fails_and_writes_nothing(checkout: Path):
    tables = _vllm_tables()
    tables["address"]["port"] = 99999
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code != 0
    assert "PORT" in result.output
    _no_env_files(checkout)


def test_a_wrongly_typed_answer_fails_and_writes_nothing(checkout: Path):
    tables = _vllm_tables()
    tables["address"]["port"] = True
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code != 0
    assert "address.port" in result.output
    _no_env_files(checkout)

    tables = _vllm_tables()
    tables["login"]["dev_mode"] = 1
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code != 0
    assert "login.dev_mode" in result.output
    _no_env_files(checkout)


def test_dev_mode_with_a_public_base_url_fails_and_writes_nothing(checkout: Path):
    tables = _vllm_tables()
    tables["address"]["base_url"] = "https://example.com"
    path = _write(checkout, tables)
    result = runner.invoke(cli_app.app, ["setup", "--answers", str(path)])
    assert result.exit_code != 0
    _no_env_files(checkout)


def test_a_provider_the_validation_refuses_fails_and_writes_nothing(checkout: Path):
    tables = _vllm_tables()
    tables["provider"]["id"] = "vllm"
    del tables["provider"]["vllm_base_url"]  # vLLM needs its endpoint
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code != 0
    _no_env_files(checkout)


# --- --write-answers ------------------------------------------------------------


def test_write_answers_never_contains_a_secret(checkout: Path):
    path = checkout / "answers.toml"
    first = runner.invoke(
        cli_app.app,
        ["setup", "--write-answers", str(path)],
        input=ANTHROPIC_SETUP_ANSWERS,
    )
    assert first.exit_code == 0
    text = path.read_text(encoding="utf-8")
    assert "sk-test-123" not in text
    for secret_key in (
        "anthropic_api_key",
        "claude_code_oauth_token",
        "google_client_secret",
        "github_client_secret",
        "orcid_client_secret",
    ):
        assert f"{secret_key} =" not in text, secret_key
    tables = tomllib.loads(text)
    assert tables["provider"]["id"] == "anthropic"
    assert tables["login"]["dev_mode"] == "true"


# --- The live check honours the skip key ----------------------------------------


def test_the_skip_key_skips_the_billed_live_check(checkout: Path, monkeypatch):
    calls: list[str] = []

    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        calls.append(provider_id)
        return LiveCheckResult(ok=True, detail="ok")

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    tables = _foundry_tables()
    tables["provider"]["skip_live_check"] = True
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code == 0
    assert calls == []
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "foundry"


def test_an_empty_answer_clears_the_saved_alternative(checkout: Path):
    tables = _foundry_tables()
    tables["provider"]["skip_live_check"] = True
    first = runner.invoke(cli_app.app, ["setup", "--answers", str(_write(checkout, tables))])
    assert first.exit_code == 0, first.output
    tables["provider"]["anthropic_foundry_resource"] = ""
    tables["provider"]["anthropic_foundry_base_url"] = "https://res.services.ai.azure.com/anthropic"
    second = runner.invoke(cli_app.app, ["setup", "--answers", str(_write(checkout, tables))])
    assert second.exit_code == 0, second.output
    assert env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_RESOURCE") == ""
    assert (
        env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_BASE_URL")
        == "https://res.services.ai.azure.com/anthropic"
    )


def test_a_file_giving_both_alternatives_is_refused_and_writes_nothing(checkout: Path):
    tables = _foundry_tables()
    tables["provider"]["skip_live_check"] = True
    first = runner.invoke(cli_app.app, ["setup", "--answers", str(_write(checkout, tables))])
    assert first.exit_code == 0, first.output
    before = (checkout / ".env").read_bytes()
    tables["provider"]["anthropic_foundry_base_url"] = "https://res.services.ai.azure.com/anthropic"
    second = runner.invoke(cli_app.app, ["setup", "--answers", str(_write(checkout, tables))])
    assert second.exit_code == 1
    assert "ANTHROPIC_FOUNDRY_RESOURCE and ANTHROPIC_FOUNDRY_BASE_URL" in second.output
    assert (checkout / ".env").read_bytes() == before


def test_the_skip_key_as_a_truthy_string_skips_the_billed_live_check(checkout: Path, monkeypatch):
    calls: list[str] = []

    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        calls.append(provider_id)
        return LiveCheckResult(ok=True, detail="ok")

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    tables = _foundry_tables()
    tables["provider"]["skip_live_check"] = "true"
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code == 0, result.output
    assert calls == []


def test_the_billed_live_check_runs_without_the_skip_key(checkout: Path, monkeypatch):
    calls: list[str] = []

    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        calls.append(provider_id)
        return LiveCheckResult(ok=True, detail="one-token completion")

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, _foundry_tables()))], input=""
    )
    assert result.exit_code == 0
    assert calls == ["foundry"]


def test_a_failing_live_check_fails_and_writes_nothing(checkout: Path, monkeypatch):
    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        return LiveCheckResult(ok=False, detail="the server refused the connection")

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    tables = _foundry_tables()
    tables["provider"]["skip_live_check"] = False
    result = runner.invoke(
        cli_app.app, ["setup", "--answers", str(_write(checkout, tables))], input=""
    )
    assert result.exit_code != 0
    assert "the server refused the connection" in result.output
    _no_env_files(checkout)


def test_write_answers_records_the_skip_a_billed_check_got(checkout: Path, monkeypatch):
    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        return LiveCheckResult(ok=True, detail="ok")

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    path = checkout / "answers.toml"
    # Provider 5 is foundry. Its four questions, then the declined billed check,
    # then every later question at its default.
    input_lines = ["5", "res", "", "key", "", "n", *wizard_tail()]
    result = runner.invoke(
        cli_app.app,
        ["setup", "--write-answers", str(path)],
        input="\n".join(input_lines) + "\n",
    )
    assert result.exit_code == 0
    tables = tomllib.loads(path.read_text(encoding="utf-8"))
    assert tables["provider"]["skip_live_check"] is True
    # An answers run without the skip key runs the billed check, and the file
    # it writes records that it ran.
    result = runner.invoke(
        cli_app.app,
        [
            "setup",
            "--answers",
            str(_write(checkout, _foundry_tables(), name="replay.toml")),
            "--write-answers",
            str(path),
        ],
        input="",
    )
    assert result.exit_code == 0
    tables = tomllib.loads(path.read_text(encoding="utf-8"))
    assert tables["provider"]["skip_live_check"] is False


# --- The file's own format ----------------------------------------------------


def test_write_answers_drops_an_empty_step(tmp_path: Path):
    path = tmp_path / "answers.toml"
    answers_io.write_answers(path, {"provider": {"id": "vllm"}, "login": {}})
    assert "[login]" not in path.read_text(encoding="utf-8")
    assert answers_io.read_answers(path) == {"provider": {"id": "vllm"}}


def test_write_answers_round_trips_values_with_special_characters(tmp_path: Path):
    value = 'a"b\\c\nd\re\tf\x01g\x7fh'
    path = tmp_path / "answers.toml"
    answers_io.write_answers(path, {"provider": {"id": value}})
    text = path.read_text(encoding="utf-8")
    for char in ("\r", "\t", "\x01", "\x7f"):
        assert char not in text, repr(char)
    assert answers_io.read_answers(path) == {"provider": {"id": value}}

"""Setup re-runs: switching between alternative answers and clearing saved values."""

from pathlib import Path

from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles
from tests.cli.conftest import env_value, wizard_input, wizard_tail

runner = CliRunner()

FOUNDRY_URL = "https://res.services.ai.azure.com/anthropic"


def _setup(answers: str) -> str:
    result = runner.invoke(cli_app.app, ["setup"], input=answers)
    assert result.exit_code == 0, result.output
    return result.output


def _rerun(provider_answers: list[str], tail: list[str] | None = None) -> str:
    """A re-run: the provider choice is already saved, so the provider's own
    questions come first, then the later steps, at their saved values unless given."""
    lines = [*provider_answers, *(tail if tail is not None else [""] * len(wizard_tail()))]
    return _setup("\n".join(lines) + "\n")


def _assigned(checkout: Path, key: str) -> bool:
    """Whether .env holds an uncommented assignment of ``key``, empty or not."""
    lines = (checkout / ".env").read_text(encoding="utf-8").splitlines()
    return any(line.startswith(f"{key}=") for line in lines)


def test_switching_foundry_from_resource_to_url_keeps_only_the_url(checkout: Path, monkeypatch):
    _setup(wizard_input("5", ["res", "", "key", "", "n"]))
    _rerun(["", FOUNDRY_URL, "", "", "n"])
    assert env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_RESOURCE") == ""
    assert _assigned(checkout, "ANTHROPIC_FOUNDRY_RESOURCE")
    assert env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_BASE_URL") == FOUNDRY_URL
    # A resource still exported in the shell never reaches compose over the cleared one.
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "res")
    assert "ANTHROPIC_FOUNDRY_RESOURCE" not in envfiles.docker_env(checkout)


def test_switching_foundry_from_url_to_resource_keeps_only_the_resource(checkout: Path):
    _setup(wizard_input("5", ["", FOUNDRY_URL, "key", "", "n"]))
    _rerun(["res", "", "", "", "n"])
    assert env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_RESOURCE") == "res"
    assert env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_BASE_URL") == ""


def test_switching_anthropic_from_api_key_to_oauth_token_keeps_only_the_token(checkout: Path):
    _setup(wizard_input("1", ["sk-test", "", ""]))
    _rerun(["", "oauth-token", ""])
    assert env_value(checkout, ".env", "ANTHROPIC_API_KEY") == ""
    assert env_value(checkout, ".env", "CLAUDE_CODE_OAUTH_TOKEN") == "oauth-token"


def test_switching_bedrock_from_bearer_token_to_access_keys_keeps_only_the_keys(checkout: Path):
    model = "us.anthropic.claude-sonnet-5-5-v2:0"
    _setup(wizard_input("4", ["us-east-1", "", "", "", "bearer", model]))
    _rerun(["", "AKIATEST", "secret", "", "", ""])
    assert env_value(checkout, ".env", "AWS_BEARER_TOKEN_BEDROCK") == ""
    assert env_value(checkout, ".env", "AWS_ACCESS_KEY_ID") == "AKIATEST"
    assert env_value(checkout, ".env", "AWS_SECRET_ACCESS_KEY") == "secret"


def test_switching_openai_from_api_key_to_chatgpt_login_keeps_only_the_login(
    checkout: Path, tmp_path: Path
):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    _setup(wizard_input("6", ["sk-openai", "", ""]))
    _rerun(["", str(auth), ""])
    assert env_value(checkout, ".env", "OPENAI_API_KEY") == ""
    assert env_value(checkout, ".env", "CODEX_AUTH_HOST_PATH") == str(auth)


def _phenix_install(tmp_path: Path) -> Path:
    install = tmp_path / "phenix"
    (install / "bin").mkdir(parents=True)
    (install / "bin" / "phenix.about").write_text("")
    return install


def test_a_dash_clears_saved_text_path_and_secret_answers(
    checkout: Path, tmp_path: Path, monkeypatch
):
    phenix = _phenix_install(tmp_path)
    tail = wizard_tail()
    tail[6], tail[7] = "google-id", "google-secret"  # GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    tail[15] = str(phenix)  # PHENIX_HOST_PATH
    _setup("\n".join(["5", "res", "", "key", "claude-x", "n", *tail]) + "\n")
    assert env_value(checkout, ".env", "GOOGLE_CLIENT_SECRET") == "google-secret"

    rerun_tail = [""] * len(tail)
    rerun_tail[6], rerun_tail[7], rerun_tail[15] = "-", "-", "-"
    _rerun(["", "", "", "-", "n"], rerun_tail)

    saved = envfiles.current_values(checkout)
    for key in (
        "OPENSCIENTIST_MODEL",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "PHENIX_HOST_PATH",
    ):
        assert key not in saved, key
        assert _assigned(checkout, key), key
    # A Phenix path still exported in the shell never reaches compose over the cleared one.
    monkeypatch.setenv("PHENIX_HOST_PATH", str(phenix))
    assert "PHENIX_HOST_PATH" not in envfiles.docker_env(checkout)


def test_both_foundry_endpoints_saved_are_rejected_until_one_is_cleared(checkout: Path):
    _setup(wizard_input("5", ["res", "", "key", "", "n"]))
    envfiles.set_file_value(checkout, "env", "ANTHROPIC_FOUNDRY_BASE_URL", FOUNDRY_URL)
    # Enter keeps both and is refused, then a dash clears the resource.
    output = _rerun(["", "", "", "", "-", "", "", "", "n"])
    assert "ANTHROPIC_FOUNDRY_RESOURCE" in output
    assert "Those answers do not validate" in output
    assert env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_RESOURCE") == ""
    assert env_value(checkout, ".env", "ANTHROPIC_FOUNDRY_BASE_URL") == FOUNDRY_URL

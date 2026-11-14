"""Setup re-runs: clearing saved answers."""

from pathlib import Path

from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import envfiles
from tests.cli.conftest import env_value, wizard_tail

runner = CliRunner()


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

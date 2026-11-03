"""The Phenix step: re-asks on a path that is not an install, accepts a real install tree."""

from pathlib import Path

from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli.steps.phenix import PhenixStep
from tests.cli.conftest import env_value

runner = CliRunner()


def _phenix_tree(root: Path) -> Path:
    """A minimal Phenix 2.x install layout: bin/phenix.about."""
    tree = root / "phenix-2.0-5936"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "phenix.about").write_text("Phenix 2.0", encoding="utf-8")
    return tree


def _head() -> list[str]:
    return [
        "1",
        "sk-test-123",
        "",  # CLAUDE_CODE_OAUTH_TOKEN
        "",  # OPENSCIENTIST_MODEL
        "",  # OPENSCIENTIST_BASE_URL
        "",  # PORT
        "",  # OPENSCIENTIST_BIND_ADDRESS
        "",  # POSTGRES_PORT
        "",  # OPENSCIENTIST_HARNESS
        "",  # OPENSCIENTIST_MAX_CONCURRENT_JOBS
        "",  # GOOGLE_CLIENT_ID
        "",  # GOOGLE_CLIENT_SECRET
        "",  # GITHUB_CLIENT_ID
        "",  # GITHUB_CLIENT_SECRET
        "",  # ORCID_CLIENT_ID
        "",  # ORCID_CLIENT_SECRET
        "true",  # OPENSCIENTIST_DEV_MODE
        "",  # BOOTSTRAP_ADMIN_EMAILS
        "",  # OPENSCIENTIST_AIRGAPPED
    ]


def test_phenix_reasks_on_a_path_that_is_not_an_install(checkout: Path):
    valid = _phenix_tree(checkout)
    lines = [
        *_head(),
        "/tmp/no-such-phenix-install",  # phenix round one: not an install
        str(valid),  # phenix round two: the real install tree
    ]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    # The rejected first pass was re-asked. The second pass's valid tree is what
    # got saved.
    assert env_value(checkout, ".env", "PHENIX_HOST_PATH") == str(valid)


def test_phenix_accepts_a_real_install_tree(checkout: Path):
    valid = _phenix_tree(checkout)
    lines = [
        *_head(),
        str(valid),  # phenix: the real install tree
    ]
    # The input holds exactly one phenix answer, so a re-ask would run out of
    # input and abort the run. The clean exit proves the first pass was
    # accepted without a re-ask.
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert env_value(checkout, ".env", "PHENIX_HOST_PATH") == str(valid)


def test_validate_rejects_a_relative_path():
    errors = PhenixStep().validate({"PHENIX_HOST_PATH": "phenix-2.0-5936"})
    assert len(errors) == 1
    assert "PHENIX_HOST_PATH" in errors[0]


def test_validate_rejects_path_traversal():
    errors = PhenixStep().validate({"PHENIX_HOST_PATH": "/opt/phenix/../../etc"})
    assert len(errors) == 1
    assert "PHENIX_HOST_PATH" in errors[0]

"""The login step: the method requirement, the callback URLs, and the OAuth pairs."""

from pathlib import Path

from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli.steps.login import LoginStep
from tests.cli.conftest import env_value

runner = CliRunner()


def _head(base_url: str = "") -> list[str]:
    """Provider (anthropic) through concurrency, the login step's predecessors."""
    return [
        "1",
        "sk-test-123",
        "",  # CLAUDE_CODE_OAUTH_TOKEN
        "",  # OPENSCIENTIST_MODEL
        base_url,  # OPENSCIENTIST_BASE_URL
        "",  # PORT
        "",  # OPENSCIENTIST_BIND_ADDRESS
        "",  # POSTGRES_PORT
        "",  # OPENSCIENTIST_HARNESS
        "",  # OPENSCIENTIST_MAX_CONCURRENT_JOBS
    ]


def test_login_refuses_no_method_until_one_is_set(checkout: Path):
    lines = [
        *_head(),
        # Login first pass: no method (all empty, dev mode defaults to False).
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        # Login second pass: the dev mock login.
        "",
        "",
        "",
        "",
        "",
        "",
        "true",
        "",
        "",  # OPENSCIENTIST_AIRGAPPED
        "",  # PHENIX_HOST_PATH
    ]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    # The rejected first pass was re-asked. The second pass's dev mock login is
    # what got saved, and the run consumed both passes through to the tail.
    assert env_value(checkout, ".env", "OPENSCIENTIST_DEV_MODE") == "true"
    assert env_value(checkout, ".env", "OPENSCIENTIST_AIRGAPPED") == "False"
    assert env_value(checkout, ".env", "PHENIX_HOST_PATH") == ""


def test_login_shows_the_callback_url_for_each_provider(checkout: Path):
    # A public base URL, so the dev mock login is not among the questions.
    lines = [
        *_head("https://example.com"),
        "google-id",
        "google-secret",
        "",
        "",
        "",
        "",
        "",  # BOOTSTRAP_ADMIN_EMAILS
        "",  # OPENSCIENTIST_AIRGAPPED
        "",  # PHENIX_HOST_PATH
    ]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert "https://example.com/auth/google/callback" in result.output
    assert "https://example.com/auth/github/callback" in result.output
    assert "https://example.com/auth/orcid/callback" in result.output


def test_login_refuses_an_unpaired_oauth_client(checkout: Path):
    lines = [
        *_head(),
        # Login first pass: a client id without its secret.
        "google-id",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        # Login second pass: the pair completed.
        "google-id",
        "google-secret",
        "",
        "",
        "",
        "",
        "",
        "",
        "",  # OPENSCIENTIST_AIRGAPPED
        "",  # PHENIX_HOST_PATH
    ]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    # The rejected first pass (id without secret) was re-asked. The completed
    # pair is what got saved, and the run consumed both passes through to the
    # tail.
    assert env_value(checkout, ".env", "GOOGLE_CLIENT_ID") == "google-id"
    assert env_value(checkout, ".env", "GOOGLE_CLIENT_SECRET") == "google-secret"
    assert env_value(checkout, ".env", "OPENSCIENTIST_DEV_MODE") == "False"


def test_login_does_not_offer_dev_mode_for_a_public_base_url(checkout: Path):
    # The input answers the OAuth pair and nothing else. An extra dev-mode
    # question would run the input out and abort the run.
    lines = [
        *_head("https://example.com"),
        "google-id",
        "google-secret",
        "",
        "",
        "",
        "",
        "",  # BOOTSTRAP_ADMIN_EMAILS
        "",  # OPENSCIENTIST_AIRGAPPED
        "",  # PHENIX_HOST_PATH
    ]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert env_value(checkout, ".env", "GOOGLE_CLIENT_ID") == "google-id"
    # The dev mock login keeps the example's off value, never written by the run.
    assert env_value(checkout, ".env", "OPENSCIENTIST_DEV_MODE") == "false"


def test_login_offers_dev_mode_only_for_a_local_url():
    local = [
        q.env for q in LoginStep().questions({"OPENSCIENTIST_BASE_URL": "http://localhost:8080"})
    ]
    assert "OPENSCIENTIST_DEV_MODE" in local
    public = [
        q.env for q in LoginStep().questions({"OPENSCIENTIST_BASE_URL": "https://example.com"})
    ]
    assert "OPENSCIENTIST_DEV_MODE" not in public
    # A current dev-mode value is still asked, so it can be turned off.
    kept = [
        q.env
        for q in LoginStep().questions(
            {"OPENSCIENTIST_BASE_URL": "https://example.com", "OPENSCIENTIST_DEV_MODE": "true"}
        )
    ]
    assert "OPENSCIENTIST_DEV_MODE" in kept


def test_login_validate_refuses_dev_mode_with_a_public_base_url():
    errors = LoginStep().validate(
        {"OPENSCIENTIST_BASE_URL": "https://example.com", "OPENSCIENTIST_DEV_MODE": "true"}
    )
    assert len(errors) == 1
    assert "OPENSCIENTIST_DEV_MODE" in errors[0]


def test_login_no_method_error_suggests_dev_mode_for_a_local_url():
    errors = LoginStep().validate({"OPENSCIENTIST_BASE_URL": "http://localhost:8080"})
    assert len(errors) == 1
    assert "OPENSCIENTIST_DEV_MODE" in errors[0]


def test_login_no_method_error_omits_dev_mode_for_a_public_url():
    errors = LoginStep().validate({"OPENSCIENTIST_BASE_URL": "https://example.com"})
    assert len(errors) == 1
    assert "OPENSCIENTIST_DEV_MODE" not in errors[0]

"""The air-gapped step: offered for an air-gappable provider, not for one whose posture is UNSUPPORTED."""

from pathlib import Path

from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from tests.cli.conftest import env_value

runner = CliRunner()

_LOGIN = ["", "", "", "", "", "", "true", ""]


def test_airgap_offered_for_a_proxied_provider(checkout: Path):
    lines = [
        "9",  # vLLM
        "http://host.docker.internal:8000/v1",  # VLLM_BASE_URL
        "",  # VLLM_API_KEY
        "qwen3-8b",  # OPENSCIENTIST_MODEL
        "",  # OPENSCIENTIST_BASE_URL
        "",  # PORT
        "",  # POSTGRES_PORT
        "",  # OPENSCIENTIST_HARNESS
        "",  # OPENSCIENTIST_MAX_CONCURRENT_JOBS
        *_LOGIN,  # login: dev mock
        "",  # OPENSCIENTIST_AIRGAPPED
        "",  # PHENIX_HOST_PATH
    ]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert "OPENSCIENTIST_AIRGAPPED" in result.output
    assert env_value(checkout, ".env", "OPENSCIENTIST_AIRGAPPED") == "False"


def test_airgap_not_offered_for_an_unsupported_posture(checkout: Path):
    # OpenAI with a ChatGPT login (no API key) cannot be air-gapped.
    lines = [
        "6",  # OpenAI API
        "",  # OPENAI_API_KEY
        "/tmp/codex-auth.json",  # CODEX_AUTH_HOST_PATH
        "",  # OPENSCIENTIST_MODEL
        "",  # OPENSCIENTIST_BASE_URL
        "",  # PORT
        "",  # POSTGRES_PORT
        "",  # OPENSCIENTIST_HARNESS
        "",  # OPENSCIENTIST_MAX_CONCURRENT_JOBS
        *_LOGIN,  # login: dev mock
        "",  # PHENIX_HOST_PATH
    ]
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert "OPENSCIENTIST_AIRGAPPED" not in result.output
    assert env_value(checkout, ".env", "OPENSCIENTIST_AIRGAPPED") == ""

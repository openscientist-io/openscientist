"""The live provider check: the failure choices, the billed skip, and the per-provider calls."""

from pathlib import Path

import requests
from typer.testing import CliRunner

from openscientist.cli import app as cli_app
from openscientist.cli import livecheck
from openscientist.providers import provider_class
from openscientist.providers.base import LiveCheckResult
from tests.cli.conftest import env_value, wizard_tail

runner = CliRunner()


def _provider_round(provider_id: str, provider_answers: list[str]) -> list[str]:
    return [provider_id, *provider_answers]


# --- The wizard's failure choices -------------------------------------------


def test_save_anyway_on_live_check_failure(checkout: Path, monkeypatch):
    runs: list[str] = []

    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        runs.append(provider_id)
        return LiveCheckResult(ok=False, detail="fake failure")

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    lines = _provider_round("1", ["sk-test-123", "", ""]) + ["s"] + wizard_tail()
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert "fake failure" in result.output
    # Saving anyway: the check ran once (no retry) and the provider's answers
    # were kept.
    assert runs == ["anthropic"]
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "anthropic"
    assert env_value(checkout, ".env", "ANTHROPIC_API_KEY") == "sk-test-123"


def test_retry_then_success(checkout: Path, monkeypatch):
    results = [
        LiveCheckResult(ok=False, detail="first attempt fails"),
        LiveCheckResult(ok=True, detail="second attempt ok"),
    ]
    runs: list[str] = []

    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        runs.append(provider_id)
        return results.pop(0)

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    lines = _provider_round("1", ["sk-test-123", "", ""]) + ["r"] + wizard_tail()
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert "first attempt fails" in result.output
    assert "second attempt ok" in result.output
    # Retrying re-runs the same check. It ran exactly twice, then the run
    # finished and saved the settings.
    assert runs == ["anthropic", "anthropic"]
    assert results == []
    assert env_value(checkout, ".env", "OPENSCIENTIST_PROVIDER") == "anthropic"


def test_fix_reasks_the_provider_questions(checkout: Path, monkeypatch):
    results = [
        LiveCheckResult(ok=False, detail="first attempt fails"),
        LiveCheckResult(ok=True, detail="fixed and ok"),
    ]
    runs: list[str] = []

    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        runs.append(provider_id)
        return results.pop(0)

    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    # Round one: the provider questions. Then 'f' to fix. Round two: the provider
    # questions again (not the choice), with a corrected key.
    lines = (
        _provider_round("1", ["sk-test-123", "", ""])
        + ["f"]
        + ["sk-corrected", "", ""]
        + wizard_tail()
    )
    result = runner.invoke(cli_app.app, ["setup"], input="\n".join(lines) + "\n")
    assert result.exit_code == 0
    assert "first attempt fails" in result.output
    assert "fixed and ok" in result.output
    # Fixing re-asked the provider questions and re-ran the check, which then
    # passed. The corrected key is what got saved.
    assert runs == ["anthropic", "anthropic"]
    assert results == []
    assert env_value(checkout, ".env", "ANTHROPIC_API_KEY") == "sk-corrected"


# --- The check runs from the host --------------------------------------------


def test_the_check_reaches_the_containers_host_name_through_loopback():
    """A URL written for the containers is probed from the host at 127.0.0.1 when the host cannot resolve the name."""
    answers = {
        "VLLM_BASE_URL": "http://host.docker.internal:43524/v1",
        "OPENSCIENTIST_MODEL": "m",
    }
    seen = livecheck.host_view(answers, resolves=lambda name: False)
    assert seen == {"VLLM_BASE_URL": "http://127.0.0.1:43524/v1", "OPENSCIENTIST_MODEL": "m"}
    assert livecheck.host_view(answers, resolves=lambda name: True) == answers
    assert answers["VLLM_BASE_URL"] == "http://host.docker.internal:43524/v1"


def test_the_check_probes_loopback_and_leaves_the_answers_alone(monkeypatch):
    """The probe goes to 127.0.0.1, while the answers the wizard saves keep the containers' URL."""
    probed: list[dict[str, str]] = []

    def fake(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        probed.append(answers)
        return LiveCheckResult(ok=True, detail="ok")

    monkeypatch.setattr(livecheck, "_resolves", lambda name: False)
    monkeypatch.setattr(livecheck, "perform_live_check", fake)
    answers = {"VLLM_BASE_URL": "http://host.docker.internal:43524/v1"}
    livecheck.run_live_check("vllm", answers)
    assert probed == [{"VLLM_BASE_URL": "http://127.0.0.1:43524/v1"}]
    assert answers == {"VLLM_BASE_URL": "http://host.docker.internal:43524/v1"}


# --- The billed check offers skip --------------------------------------------


def test_billed_check_offers_skip(monkeypatch):
    import click

    calls: list[str] = []

    def fake_perform(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        calls.append(provider_id)
        return LiveCheckResult(ok=True, detail="ok")

    monkeypatch.setattr(livecheck, "perform_live_check", fake_perform)
    prompts = iter(["n"])  # decline the billed check
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))
    outcome = livecheck.run_live_check("vertex", {"OPENSCIENTIST_PROVIDER": "vertex"})
    assert outcome == "pass"
    assert calls == []  # declined, so no billed request was made


def test_billed_check_runs_when_accepted(monkeypatch):
    import click

    calls: list[str] = []

    def fake_perform(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
        calls.append(provider_id)
        return LiveCheckResult(ok=True, detail="one-token completion")

    monkeypatch.setattr(livecheck, "perform_live_check", fake_perform)
    prompts = iter(["y"])  # accept the billed check
    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: next(prompts))
    outcome = livecheck.run_live_check("foundry", {"OPENSCIENTIST_PROVIDER": "foundry"})
    assert outcome == "pass"
    assert calls == ["foundry"]


# --- The per-provider calls, with the network faked at its boundary -----------


class _FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def test_anthropic_live_check_reads_the_model_listing(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: _FakeResponse(200, {"data": [{"id": "claude-sonnet-5-5"}]}),
    )
    result = provider_class("anthropic").live_check(
        "anthropic", {"ANTHROPIC_API_KEY": "sk-secret-123"}
    )
    assert result.ok
    assert "1 models" in result.detail
    assert "sk-secret-123" not in result.detail  # never echo the secret


def test_anthropic_live_check_surfaces_the_error_without_the_secret(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _FakeResponse(401, {}))
    result = provider_class("anthropic").live_check(
        "anthropic", {"ANTHROPIC_API_KEY": "sk-secret-123"}
    )
    assert not result.ok
    assert "401" in result.detail
    assert "sk-secret-123" not in result.detail


def test_vllm_live_check_reports_an_unreachable_server(monkeypatch):
    def raise_connection_error(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("requests.get", raise_connection_error)
    result = provider_class("vllm").live_check(
        "vllm", {"VLLM_BASE_URL": "http://localhost:9/v1", "OPENSCIENTIST_MODEL": "qwen"}
    )
    assert not result.ok
    assert "Could not reach the vLLM server" in result.detail


def test_vllm_live_check_reads_the_context_window(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: _FakeResponse(
            200, {"data": [{"id": "qwen", "max_model_len": 4096}]}
        ),
    )
    result = provider_class("vllm").live_check(
        "vllm", {"VLLM_BASE_URL": "http://localhost:9/v1", "OPENSCIENTIST_MODEL": "qwen"}
    )
    assert result.ok
    assert "4096" in result.detail

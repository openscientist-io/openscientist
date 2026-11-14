"""The live provider check: the failure choices, the billed skip, and the per-provider calls."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    assert outcome.verdict == "pass"
    assert outcome.billed_check_skipped
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
    assert outcome.verdict == "pass"
    assert not outcome.billed_check_skipped
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


def test_a_failed_listing_shows_the_providers_reason(monkeypatch):
    # Azure's answer to a dated api-version on the v1 surface.
    reason = {"error": {"code": "BadRequest", "message": "API version not supported"}}
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _FakeResponse(400, reason))
    result = provider_class("azure-openai").live_check(
        "azure-openai", {"AZURE_OPENAI_RESOURCE": "res1", "AZURE_OPENAI_API_KEY": "az-key"}
    )
    assert not result.ok
    assert "HTTP 400" in result.detail
    assert "API version not supported" in result.detail


def test_a_failed_completion_shows_the_providers_reason(monkeypatch):
    # The Messages API's answer to a request without its version header.
    reason = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "anthropic-version: header is required",
        },
    }
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: _FakeResponse(400, reason))
    result = provider_class("foundry").live_check(
        "foundry",
        {
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://f.example.com/anthropic",
            "ANTHROPIC_FOUNDRY_API_KEY": "fk",
        },
    )
    assert not result.ok
    assert "anthropic-version: header is required" in result.detail


def test_a_reason_echoing_the_credential_never_shows_it(monkeypatch):
    reason = {"error": {"message": "Key sk-secret-123456 is not valid for this resource"}}
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _FakeResponse(401, reason))
    result = provider_class("openai").live_check("openai", {"OPENAI_API_KEY": "sk-secret-123456"})
    assert not result.ok
    assert "is not valid for this resource" in result.detail
    assert "sk-secret-123456" not in result.detail


def test_a_failure_without_a_json_reason_shows_only_the_status(monkeypatch):
    class _GatewayPage:
        status_code = 502

        def json(self):
            raise ValueError("an HTML error page")

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _GatewayPage())
    result = provider_class("openai").live_check("openai", {"OPENAI_API_KEY": "sk-test"})
    assert not result.ok
    assert result.detail == "OpenAI API listing returned HTTP 502."


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


# --- The boundary the wizard crosses, and the resolver -------------------------


def test_resolves_maps_dns_lookup_success_and_failure():
    assert livecheck._resolves("localhost") is True
    assert livecheck._resolves("no-such-host.invalid") is False


def test_perform_live_check_reaches_the_provider_own_check(monkeypatch):
    """The boundary hands the wizard's answers to the provider's own live check."""
    monkeypatch.undo()  # drop the conftest fake so the real boundary runs
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: _FakeResponse(200, {"data": [{"id": "claude-sonnet-5-5"}]}),
    )
    result = livecheck.perform_live_check("anthropic", {"ANTHROPIC_API_KEY": "sk-test"})
    assert result.ok
    assert "1 models" in result.detail


# --- The remaining providers, with the network faked at its boundary -----------


def _capture_get(monkeypatch, payload: dict | None = None, status: int = 200):
    """Fake requests.get, recording the URL and headers of each call."""
    calls: list[tuple[str, dict[str, str]]] = []

    def get(url, headers=None, timeout=None):
        calls.append((url, dict(headers or {})))
        return _FakeResponse(status, payload)

    monkeypatch.setattr("requests.get", get)
    return calls


def _capture_post(monkeypatch, response):
    """Fake requests.post, recording the URL, headers and body of each call."""
    calls: list[tuple[str, dict[str, str], object]] = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append((url, dict(headers or {}), json))
        return response

    monkeypatch.setattr("requests.post", post)
    return calls


def _unreachable_get(*args, **kwargs):
    raise requests.ConnectionError("connection refused")


def test_openai_live_check_skips_without_an_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = _capture_get(monkeypatch)
    result = provider_class("openai").live_check("openai", {})
    assert result.ok
    assert calls == []


def test_openai_live_check_calls_the_model_listing(monkeypatch):
    calls = _capture_get(monkeypatch, {"data": [{"id": "gpt-4o"}]})
    result = provider_class("openai").live_check("openai", {"OPENAI_API_KEY": "sk-test"})
    assert result.ok
    url, headers = calls[0]
    assert url == "https://api.openai.com/v1/models"
    assert headers == {"authorization": "Bearer sk-test"}


def test_anthropic_live_check_skips_without_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    calls = _capture_get(monkeypatch)
    result = provider_class("anthropic").live_check("anthropic", {})
    assert result.ok
    assert calls == []


def test_anthropic_live_check_uses_the_oauth_token_header(monkeypatch):
    calls = _capture_get(monkeypatch, {"data": []})
    result = provider_class("anthropic").live_check(
        "anthropic", {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok"}
    )
    assert result.ok
    url, headers = calls[0]
    assert url == "https://api.anthropic.com/v1/models"
    assert headers == {"authorization": "Bearer oauth-tok"}


def test_anthropic_live_check_reports_an_unreachable_server(monkeypatch):
    monkeypatch.setattr("requests.get", _unreachable_get)
    result = provider_class("anthropic").live_check("anthropic", {"ANTHROPIC_API_KEY": "sk-test"})
    assert not result.ok


class _NonJsonResponse:
    status_code = 200

    def json(self):
        raise ValueError("body is not JSON")


def test_anthropic_live_check_reports_a_non_json_listing(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _NonJsonResponse())
    result = provider_class("anthropic").live_check("anthropic", {"ANTHROPIC_API_KEY": "sk-test"})
    assert not result.ok


def test_anthropic_live_check_accepts_a_listing_without_model_data(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _FakeResponse(200, {}))
    result = provider_class("anthropic").live_check("anthropic", {"ANTHROPIC_API_KEY": "sk-test"})
    assert result.ok


def test_azure_openai_live_check_skips_without_a_resource(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_RESOURCE", raising=False)
    calls = _capture_get(monkeypatch)
    result = provider_class("azure-openai").live_check("azure-openai", {})
    assert result.ok
    assert calls == []


def test_azure_openai_live_check_lists_models_on_the_v1_surface(monkeypatch):
    calls = _capture_get(monkeypatch, {"data": []})
    result = provider_class("azure-openai").live_check(
        "azure-openai",
        {"AZURE_OPENAI_RESOURCE": "res1", "AZURE_OPENAI_API_KEY": "az-key"},
    )
    assert result.ok
    url, headers = calls[0]
    assert url == "https://res1.openai.azure.com/openai/v1/models"
    assert headers == {"api-key": "az-key"}


def test_azure_openai_live_check_sends_the_api_version_the_harness_sends(monkeypatch):
    calls = _capture_get(monkeypatch, {"data": []})
    result = provider_class("azure-openai").live_check(
        "azure-openai",
        {
            "AZURE_OPENAI_RESOURCE": "res1",
            "AZURE_OPENAI_API_KEY": "az-key",
            "AZURE_OPENAI_API_VERSION": "preview",
        },
    )
    assert result.ok
    assert calls[0][0] == "https://res1.openai.azure.com/openai/v1/models?api-version=preview"


def _fake_vertex_session(post_result):
    class _Session:
        def __init__(self, credentials):
            self.credentials = credentials

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None, timeout=None):
            return post_result(url, json, timeout)

    return _Session


def test_vertex_live_check_skips_without_a_project(monkeypatch):
    import google.auth

    calls: list[str] = []

    def default(scopes=None):
        calls.append(scopes or "")
        return object(), "proj-1"

    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    monkeypatch.setattr(google.auth, "default", default)
    result = provider_class("vertex").live_check("vertex", {})
    assert result.ok
    assert calls == []


def test_vertex_live_check_posts_a_one_token_completion(monkeypatch):
    import google.auth
    import google.auth.transport.requests as vertex_transport

    seen: dict[str, object] = {}

    def post(url, json=None, timeout=None):
        seen["url"] = url
        seen["body"] = json
        return _FakeResponse(200, {})

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "proj-1"))
    monkeypatch.setattr(vertex_transport, "AuthorizedSession", _fake_vertex_session(post))
    result = provider_class("vertex").live_check(
        "vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-1", "CLOUD_ML_REGION": "us-east5"}
    )
    assert result.ok
    assert seen["url"] == (
        "https://aiplatform.googleapis.com/v1/projects/proj-1/locations/us-east5"
        "/publishers/anthropic/models/claude-sonnet-5-5:rawPredict"
    )
    assert seen["body"]["max_tokens"] == 1


def test_vertex_live_check_reports_an_http_error(monkeypatch):
    import google.auth
    import google.auth.transport.requests as vertex_transport

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "proj-1"))
    monkeypatch.setattr(
        vertex_transport,
        "AuthorizedSession",
        _fake_vertex_session(lambda *args, **kwargs: _FakeResponse(503, {})),
    )
    result = provider_class("vertex").live_check(
        "vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-1", "CLOUD_ML_REGION": "us-east5"}
    )
    assert not result.ok
    assert "503" in result.detail


def test_vertex_live_check_reports_an_unreachable_endpoint(monkeypatch):
    import google.auth
    import google.auth.transport.requests as vertex_transport

    def post(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "proj-1"))
    monkeypatch.setattr(vertex_transport, "AuthorizedSession", _fake_vertex_session(post))
    result = provider_class("vertex").live_check(
        "vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-1", "CLOUD_ML_REGION": "us-east5"}
    )
    assert not result.ok


def test_vertex_live_check_reports_failed_credentials(monkeypatch):
    import google.auth

    def no_credentials(scopes=None):
        raise Exception("no default credentials found")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    result = provider_class("vertex").live_check(
        "vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-1", "CLOUD_ML_REGION": "us-east5"}
    )
    assert not result.ok


def test_vertex_live_check_reports_a_missing_google_auth():
    with patch.dict(sys.modules, {"google.auth": None}):
        result = provider_class("vertex").live_check(
            "vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "proj-1", "CLOUD_ML_REGION": "us-east5"}
        )
    assert not result.ok
    assert "google-auth" in result.detail


def test_bedrock_live_check_skips_without_a_region(monkeypatch):
    import boto3  # type: ignore[import-untyped]

    calls: list[str] = []

    def client(*args, **kwargs):
        calls.append("bedrock-runtime")
        return MagicMock()

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setattr(boto3, "client", client)
    result = provider_class("bedrock").live_check("bedrock", {})
    assert result.ok
    assert calls == []


def test_bedrock_live_check_lists_foundation_models(monkeypatch):
    import boto3  # type: ignore[import-untyped]

    client = MagicMock()
    client.list_foundation_models.return_value = {}
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    result = provider_class("bedrock").live_check("bedrock", {"AWS_REGION": "us-east-1"})
    assert result.ok
    client.list_foundation_models.assert_called_once_with()


def test_bedrock_live_check_reports_a_failed_listing(monkeypatch):
    import boto3  # type: ignore[import-untyped]

    client = MagicMock()
    client.list_foundation_models.side_effect = Exception("auth denied")
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    result = provider_class("bedrock").live_check("bedrock", {"AWS_REGION": "us-east-1"})
    assert not result.ok


def test_bedrock_live_check_reports_a_missing_boto3():
    with patch.dict(sys.modules, {"boto3": None}):
        result = provider_class("bedrock").live_check("bedrock", {"AWS_REGION": "us-east-1"})
    assert not result.ok
    assert "boto3" in result.detail


def test_cborg_live_check_skips_without_a_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    calls = _capture_get(monkeypatch)
    result = provider_class("cborg").live_check("cborg", {})
    assert result.ok
    assert calls == []


def test_cborg_live_check_calls_the_gateway_listing(monkeypatch):
    calls = _capture_get(monkeypatch, {"data": []})
    result = provider_class("cborg").live_check(
        "cborg",
        {
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            "ANTHROPIC_AUTH_TOKEN": "cborg-tok",
        },
    )
    assert result.ok
    url, headers = calls[0]
    assert url == "https://api.cborg.lbl.gov/v1/models"
    assert headers == {"authorization": "Bearer cborg-tok"}


def test_ollama_live_check_reports_the_loaded_context_window(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: _FakeResponse(
            200, {"models": [{"name": "gpt-oss:20b", "context_length": 131072}]}
        ),
    )
    result = provider_class("ollama").live_check(
        "ollama", {"OLLAMA_BASE_URL": "http://localhost:11434/v1"}
    )
    assert result.ok
    assert "131072" in result.detail


def test_ollama_live_check_reports_an_unreachable_server(monkeypatch):
    monkeypatch.setattr("requests.get", _unreachable_get)
    result = provider_class("ollama").live_check(
        "ollama", {"OLLAMA_BASE_URL": "http://localhost:11434/v1"}
    )
    assert not result.ok


def test_llamacpp_live_check_reports_the_launched_window(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: _FakeResponse(
            200, {"default_generation_settings": {"n_ctx": 4096}}
        ),
    )
    result = provider_class("llamacpp").live_check(
        "llamacpp", {"LLAMACPP_BASE_URL": "http://localhost:8080/v1"}
    )
    assert result.ok
    assert "4096" in result.detail


def test_llamacpp_live_check_reports_an_unreachable_server(monkeypatch):
    monkeypatch.setattr("requests.get", _unreachable_get)
    result = provider_class("llamacpp").live_check(
        "llamacpp", {"LLAMACPP_BASE_URL": "http://localhost:8080/v1"}
    )
    assert not result.ok


def test_vllm_live_check_requires_a_model_name(monkeypatch):
    monkeypatch.delenv("OPENSCIENTIST_MODEL", raising=False)
    result = provider_class("vllm").live_check(
        "vllm", {"VLLM_BASE_URL": "http://localhost:8000/v1"}
    )
    assert not result.ok
    assert "OPENSCIENTIST_MODEL" in result.detail


def test_foundry_live_check_skips_without_an_endpoint(monkeypatch):
    calls = _capture_post(monkeypatch, _FakeResponse(200, {}))
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_RESOURCE", raising=False)
    result = provider_class("foundry").live_check("foundry", {})
    assert result.ok
    assert calls == []


def test_foundry_live_check_posts_a_one_token_completion(monkeypatch):
    calls = _capture_post(monkeypatch, _FakeResponse(200, {}))
    result = provider_class("foundry").live_check(
        "foundry",
        {
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://f.example.com/anthropic",
            "ANTHROPIC_FOUNDRY_API_KEY": "fk",
        },
    )
    assert result.ok
    url, headers, body = calls[0]
    assert url == "https://f.example.com/anthropic/v1/messages"
    assert headers == {"x-api-key": "fk", "anthropic-version": "2023-06-01"}
    assert body["max_tokens"] == 1


def test_foundry_live_check_builds_the_url_from_the_resource(monkeypatch):
    calls = _capture_post(monkeypatch, _FakeResponse(200, {}))
    result = provider_class("foundry").live_check(
        "foundry",
        {
            "ANTHROPIC_FOUNDRY_RESOURCE": "fres",
            "ANTHROPIC_FOUNDRY_API_KEY": "fk",
        },
    )
    assert result.ok
    assert calls[0][0] == "https://fres.services.ai.azure.com/anthropic/v1/messages"


def test_foundry_live_check_reports_an_http_error(monkeypatch):
    _capture_post(monkeypatch, _FakeResponse(403, {}))
    result = provider_class("foundry").live_check(
        "foundry",
        {
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://f.example.com/anthropic",
            "ANTHROPIC_FOUNDRY_API_KEY": "fk",
        },
    )
    assert not result.ok
    assert "403" in result.detail


def test_foundry_live_check_reports_an_unreachable_endpoint(monkeypatch):
    monkeypatch.setattr("requests.post", _unreachable_get)
    result = provider_class("foundry").live_check(
        "foundry",
        {
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://f.example.com/anthropic",
            "ANTHROPIC_FOUNDRY_API_KEY": "fk",
        },
    )
    assert not result.ok


def test_foundry_live_check_reports_a_missing_azure_identity():
    with patch.dict(sys.modules, {"azure.identity": None}):
        result = provider_class("foundry").live_check(
            "foundry", {"ANTHROPIC_FOUNDRY_BASE_URL": "https://f.example.com/anthropic"}
        )
    assert not result.ok
    assert "azure-identity" in result.detail


def test_foundry_live_check_reports_a_failed_entra_token(monkeypatch):
    import azure.identity

    class _BrokenCredential:
        def get_token(self, *args, **kwargs):
            raise Exception("no token minted")

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", _BrokenCredential)
    result = provider_class("foundry").live_check(
        "foundry", {"ANTHROPIC_FOUNDRY_BASE_URL": "https://f.example.com/anthropic"}
    )
    assert not result.ok

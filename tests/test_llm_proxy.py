"""Tests for the LLM key-replacement proxy (Claude backend)."""

import asyncio
import socket
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp
from uvicorn.config import Config

from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_container.secrets import (
    derive_job_secret,
    issue_run_credentials,
    revoke_run_credentials,
)
from openscientist.llm_proxy import container_proxy_base_url, create_llm_proxy_app
from openscientist.providers import get_provider
from openscientist.providers.base import AirgapEgress, LlmUpstream, StaticHeadersSigning
from openscientist.providers.bedrock import SigV4Signing
from openscientist.providers.vertex import GoogleAdcSigning
from openscientist.settings import Settings, get_settings
from tests.test_provider_bedrock import verify_sigv4
from tests.test_provider_vertex import FakeVertexCredentials


@pytest.fixture
def active_provider(monkeypatch):
    """Select the global provider from env and return its Provider instance."""
    reset = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_RESOURCE",
        "OPENSCIENTIST_LLM_PROXY_URL",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "OLLAMA_BASE_URL",
        "VLLM_BASE_URL",
        "OPENSCIENTIST_MODEL",
        "VLLM_API_KEY",
        "LLAMACPP_BASE_URL",
        "LLAMACPP_API_KEY",
        "CODEX_AUTH_HOST_PATH",
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "GITHUB_TOKEN",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GCP_BILLING_ACCOUNT_ID",
        "CLOUD_ML_REGION",
        "VERTEX_REGION_CLAUDE_4_5_SONNET",
        "VERTEX_REGION_CLAUDE_4_5_HAIKU",
    )

    def _select(**env: str):
        for key in reset:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", env.pop("OPENSCIENTIST_PROVIDER"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_provider()

    yield _select
    get_settings.cache_clear()


@pytest.fixture
def placeholder_for() -> Iterator[Callable[[str], str]]:
    """Issue a live run's LLM placeholder for a job, ended after the test."""
    roles: list[str] = []

    def issue(job_id: str) -> str:
        role = f"agent_{uuid4().hex}"
        roles.append(role)
        return issue_run_credentials(job_id, role, "discovery").llm_placeholder

    yield issue
    for role in roles:
        revoke_run_credentials(role)


class TestClaudeUpstream:
    """Real upstream and auth-header derivation, on the Provider hierarchy."""

    def test_anthropic_upstream(self, active_provider):
        provider = active_provider(OPENSCIENTIST_PROVIDER="anthropic", ANTHROPIC_API_KEY="real-key")
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.anthropic.com",
            StaticHeadersSigning({"x-api-key": "real-key"}),
        )

    def test_cborg_upstream_strips_trailing_slash(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="cborg",
            ANTHROPIC_AUTH_TOKEN="real-tok",
            ANTHROPIC_BASE_URL="https://api.cborg.lbl.gov/",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.cborg.lbl.gov",
            StaticHeadersSigning({"authorization": "Bearer real-tok"}),
        )

    def test_foundry_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="foundry",
            ANTHROPIC_FOUNDRY_RESOURCE="myfoundry",
            ANTHROPIC_FOUNDRY_API_KEY="fkey",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://myfoundry.services.ai.azure.com/anthropic",
            StaticHeadersSigning({"x-api-key": "fkey"}),
        )

    def test_anthropic_oauth_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="anthropic", CLAUDE_CODE_OAUTH_TOKEN="oauth-real"
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.anthropic.com",
            StaticHeadersSigning({"authorization": "Bearer oauth-real"}),
        )

    def test_bedrock_bearer_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="bt-real",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://bedrock-runtime.us-east-1.amazonaws.com",
            StaticHeadersSigning({"authorization": "Bearer bt-real"}),
        )

    def test_bedrock_sigv4_upstream_signs_with_the_web_credential(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="leaky-ak",
            AWS_SECRET_ACCESS_KEY="leaky-sk",
        )
        upstream = provider.llm_upstream()
        assert upstream is not None
        assert upstream.base_url == "https://bedrock-runtime.us-east-1.amazonaws.com"
        assert isinstance(upstream.signing, SigV4Signing)


class TestProxiedContainerEnv:
    """The job-container env transform: redirect LLM traffic, drop GITHUB_TOKEN."""

    def test_anthropic_redirects_and_drops_github(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="real-key",
            GITHUB_TOKEN="ghp_secret",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://openscientist:8081"
        assert env["ANTHROPIC_API_KEY"] == "job-1.tok"
        assert "GITHUB_TOKEN" not in env
        assert "real-key" not in env.values()

    def test_cborg_redirects(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="cborg",
            ANTHROPIC_AUTH_TOKEN="real-tok",
            ANTHROPIC_BASE_URL="https://api.cborg.lbl.gov",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://openscientist:8081"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "job-1.tok"
        assert "real-tok" not in env.values()

    def test_anthropic_oauth_redirects(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="anthropic",
            CLAUDE_CODE_OAUTH_TOKEN="oauth-real",
            GITHUB_TOKEN="ghp_secret",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://openscientist:8081"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "job-1.tok"
        assert "oauth-real" not in env.values()
        assert "GITHUB_TOKEN" not in env

    def test_bedrock_bearer_redirects_and_strips_sigv4(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="bt-real",
            AWS_ACCESS_KEY_ID="leaky-ak",
            AWS_SECRET_ACCESS_KEY="leaky-sk",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://openscientist:8081"
        assert env["AWS_BEARER_TOKEN_BEDROCK"] == "job-1.tok"
        assert "bt-real" not in env.values()
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "leaky-ak" not in env.values()

    def test_bedrock_sigv4_routes_the_container_through_the_proxy(self, active_provider):
        """The container reaches Bedrock only through the proxy and holds no AWS
        credential of its own; the gateway bearer is the run's placeholder."""
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="leaky-ak",
            AWS_SECRET_ACCESS_KEY="leaky-sk",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://openscientist:8081"
        assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "job-1.tok"
        assert env["AWS_REGION"] == "us-east-1"
        # The container holds no AWS credential, and no Bedrock bearer.
        assert "AWS_BEARER_TOKEN_BEDROCK" not in env
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "AWS_PROFILE" not in env
        assert "AWS_SESSION_TOKEN" not in env
        assert "leaky-ak" not in env.values()
        assert "leaky-sk" not in env.values()

    def test_foundry_redirects_and_drops_resource(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="foundry",
            ANTHROPIC_FOUNDRY_RESOURCE="myfoundry",
            ANTHROPIC_FOUNDRY_API_KEY="fkey",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["ANTHROPIC_FOUNDRY_BASE_URL"] == "http://openscientist:8081"
        assert env["ANTHROPIC_FOUNDRY_API_KEY"] == "job-1.tok"
        assert "ANTHROPIC_FOUNDRY_RESOURCE" not in env
        assert "fkey" not in env.values()

    def test_vertex_routes_the_container_through_the_proxy(self, active_provider, tmp_path):
        """The container reaches Vertex only through the proxy and holds no
        GCP credential of its own."""
        creds = tmp_path / "sa.json"
        creds.write_text('{"type": "service_account"}')
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="vertex",
            ANTHROPIC_VERTEX_PROJECT_ID="proj-9",
            GCP_BILLING_ACCOUNT_ID="BILL-9",
            CLOUD_ML_REGION="us-east5",
            GOOGLE_APPLICATION_CREDENTIALS=str(creds),
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert env["ANTHROPIC_VERTEX_BASE_URL"] == "http://openscientist:8081"
        assert env["CLAUDE_CODE_SKIP_VERTEX_AUTH"] == "1"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "job-1.tok"
        assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-9"
        assert env["CLOUD_ML_REGION"] == "us-east5"
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
        assert str(creds) not in env.values()


class TestCodexUpstream:
    """Codex backend upstream derivation on the Provider hierarchy."""

    def test_openai_upstream(self, active_provider):
        provider = active_provider(OPENSCIENTIST_PROVIDER="openai", OPENAI_API_KEY="sk-real")
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.openai.com/v1",
            StaticHeadersSigning({"authorization": "Bearer sk-real"}),
        )

    def test_azure_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="az-real",
            AZURE_OPENAI_RESOURCE="myres",
            OPENSCIENTIST_MODEL="gpt5",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://myres.openai.azure.com/openai/v1",
            StaticHeadersSigning({"authorization": "Bearer az-real"}),
        )

    def test_ollama_upstream_is_keyless(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="ollama", OLLAMA_BASE_URL="http://ollama:11434/v1"
        )
        assert provider.llm_upstream() == LlmUpstream(
            "http://ollama:11434/v1",
            StaticHeadersSigning({}),
        )

    def test_vllm_upstream_is_keyless_without_api_key(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="vllm",
            VLLM_BASE_URL="http://vllm:8000/v1",
            OPENSCIENTIST_MODEL="Qwen/Qwen3-32B",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "http://vllm:8000/v1",
            StaticHeadersSigning({}),
        )

    def test_vllm_upstream_injects_bearer_when_keyed(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="vllm",
            VLLM_BASE_URL="http://vllm:8000/v1",
            OPENSCIENTIST_MODEL="Qwen/Qwen3-32B",
            VLLM_API_KEY="vk-real",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "http://vllm:8000/v1",
            StaticHeadersSigning({"authorization": "Bearer vk-real"}),
        )

    def test_llamacpp_upstream_is_keyless_without_api_key(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="llamacpp",
            LLAMACPP_BASE_URL="http://llamacpp:8080/v1",
            OPENSCIENTIST_MODEL="meta-llama/Llama-3.1-8B-Instruct",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "http://llamacpp:8080/v1",
            StaticHeadersSigning({}),
        )

    def test_llamacpp_upstream_injects_bearer_when_keyed(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="llamacpp",
            LLAMACPP_BASE_URL="http://llamacpp:8080/v1",
            OPENSCIENTIST_MODEL="meta-llama/Llama-3.1-8B-Instruct",
            LLAMACPP_API_KEY="lk-real",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "http://llamacpp:8080/v1",
            StaticHeadersSigning({"authorization": "Bearer lk-real"}),
        )


class TestCodexProxiedEnv:
    """Codex env redirect: placeholder key plus the proxy URL for config.toml."""

    def test_openai_redirects(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="openai", OPENAI_API_KEY="sk-real", GITHUB_TOKEN="ghp"
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["OPENAI_API_KEY"] == "job-1.tok"
        assert env["OPENSCIENTIST_LLM_PROXY_URL"] == "http://openscientist:8081"
        assert "sk-real" not in env.values()
        assert "GITHUB_TOKEN" not in env

    def test_azure_redirects(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="az-real",
            AZURE_OPENAI_RESOURCE="myres",
            OPENSCIENTIST_MODEL="gpt5",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["AZURE_OPENAI_API_KEY"] == "job-1.tok"
        assert env["OPENSCIENTIST_LLM_PROXY_URL"] == "http://openscientist:8081"
        assert "az-real" not in env.values()

    def test_ollama_redirects(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="ollama", OLLAMA_BASE_URL="http://ollama:11434/v1"
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["OPENAI_API_KEY"] == "job-1.tok"
        assert env["OPENSCIENTIST_LLM_PROXY_URL"] == "http://openscientist:8081"

    def test_vllm_redirects(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="vllm",
            VLLM_BASE_URL="http://vllm:8000/v1",
            OPENSCIENTIST_MODEL="Qwen/Qwen3-32B",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["OPENAI_API_KEY"] == "job-1.tok"
        assert env["OPENSCIENTIST_LLM_PROXY_URL"] == "http://openscientist:8081"
        assert "VLLM_API_KEY" not in env

    def test_vllm_redirects_and_strips_the_real_key(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="vllm",
            VLLM_BASE_URL="http://vllm:8000/v1",
            OPENSCIENTIST_MODEL="Qwen/Qwen3-32B",
            VLLM_API_KEY="vk-real",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["OPENAI_API_KEY"] == "job-1.tok"
        assert env["VLLM_API_KEY"] == "job-1.tok"
        assert "vk-real" not in env.values()

    def test_llamacpp_redirects(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="llamacpp",
            LLAMACPP_BASE_URL="http://llamacpp:8080/v1",
            OPENSCIENTIST_MODEL="meta-llama/Llama-3.1-8B-Instruct",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["OPENAI_API_KEY"] == "job-1.tok"
        assert env["OPENSCIENTIST_LLM_PROXY_URL"] == "http://openscientist:8081"
        assert "LLAMACPP_API_KEY" not in env

    def test_llamacpp_redirects_and_strips_the_real_key(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="llamacpp",
            LLAMACPP_BASE_URL="http://llamacpp:8080/v1",
            OPENSCIENTIST_MODEL="meta-llama/Llama-3.1-8B-Instruct",
            LLAMACPP_API_KEY="lk-real",
        )
        env = provider.proxied_container_env(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
        assert env["OPENAI_API_KEY"] == "job-1.tok"
        assert env["LLAMACPP_API_KEY"] == "job-1.tok"
        assert "lk-real" not in env.values()


class TestCodexConfigRedirect:
    """codex config.toml base_url points at the proxy only when it is active."""

    def test_openai_config_points_at_proxy(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="openai",
            OPENAI_API_KEY="sk-real",
            OPENSCIENTIST_LLM_PROXY_URL="http://openscientist:8081",
        )
        toml = provider.codex_config_overrides()
        assert "[model_providers.openai]" in toml
        assert 'base_url = "http://openscientist:8081"' in toml
        assert 'env_key = "OPENAI_API_KEY"' in toml

    def test_openai_config_empty_without_proxy(self, active_provider):
        provider = active_provider(OPENSCIENTIST_PROVIDER="openai", OPENAI_API_KEY="sk-real")
        assert provider.codex_config_overrides() == []

    def test_azure_config_points_at_proxy(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="az-real",
            AZURE_OPENAI_RESOURCE="myres",
            OPENSCIENTIST_MODEL="gpt5",
            OPENSCIENTIST_LLM_PROXY_URL="http://openscientist:8081",
        )
        assert 'base_url = "http://openscientist:8081"' in provider.codex_config_overrides()

    def test_azure_config_uses_real_base_without_proxy(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="az-real",
            AZURE_OPENAI_RESOURCE="myres",
            OPENSCIENTIST_MODEL="gpt5",
        )
        toml = provider.codex_config_overrides()
        assert 'base_url = "https://myres.openai.azure.com/openai/v1"' in toml


class TestProxyBaseUrl:
    def test_default_and_override(self, monkeypatch):
        monkeypatch.delenv("OPENSCIENTIST_WEB_HOST", raising=False)
        assert container_proxy_base_url() == "http://openscientist:8081"
        monkeypatch.setenv("OPENSCIENTIST_WEB_HOST", "web.internal")
        assert container_proxy_base_url() == "http://web.internal:8081"


def _recording_upstream(recorded: list[dict]) -> httpx.AsyncClient:
    """An in-memory ASGI upstream that records the forwarded request and streams
    a response, so the proxy's real streaming path is exercised."""

    async def endpoint(request: Request) -> StreamingResponse:
        body = await request.body()
        recorded.append(
            {
                "method": request.method,
                "host": request.url.hostname,
                "path": request.url.path,
                "query": request.url.query,
                "headers": dict(request.headers),
                "body": body,
            }
        )

        async def stream():
            yield b"upstream-"
            yield b"body"

        return StreamingResponse(stream(), media_type="application/json")

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                endpoint,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ]
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


class TestProxyForwarding:
    """The proxy authenticates, substitutes the real key, and forwards."""

    def test_substitutes_key_and_preserves_request(self, placeholder_for):
        placeholder = placeholder_for("job-1")
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://upstream.test",
                StaticHeadersSigning({"x-api-key": "REAL-KEY"}),
            ),
            client=_recording_upstream(recorded),
        )
        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages?beta=1",
                headers={
                    "x-api-key": placeholder,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                content=b'{"m":1}',
            )
        assert resp.status_code == 200
        assert resp.content == b"upstream-body"
        assert len(recorded) == 1
        forwarded = recorded[0]
        assert forwarded["path"] == "/v1/messages"
        assert forwarded["query"] == "beta=1"
        assert forwarded["headers"]["x-api-key"] == "REAL-KEY"
        assert forwarded["headers"]["anthropic-version"] == "2023-06-01"
        assert forwarded["body"] == b'{"m":1}'

    def test_accepts_bearer_and_sets_authorization(self, placeholder_for):
        placeholder = placeholder_for("job-x")
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://cborg.test",
                StaticHeadersSigning({"authorization": "Bearer REAL-TOK"}),
            ),
            client=_recording_upstream(recorded),
        )
        with TestClient(app) as client:
            resp = client.post(
                "/v1/messages",
                headers={"authorization": f"Bearer {placeholder}"},
                content=b"{}",
            )
        assert resp.status_code == 200
        assert recorded[0]["headers"]["authorization"] == "Bearer REAL-TOK"

    def test_forwards_responses_wire_with_query(self, placeholder_for):
        placeholder = placeholder_for("job-c")
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://api.openai.com/v1",
                StaticHeadersSigning({"authorization": "Bearer REAL"}),
            ),
            client=_recording_upstream(recorded),
        )
        with TestClient(app) as client:
            resp = client.post(
                "/responses?api-version=2024",
                headers={"authorization": f"Bearer {placeholder}"},
                content=b'{"input":1}',
            )
        assert resp.status_code == 200
        assert recorded[0]["path"] == "/v1/responses"
        assert recorded[0]["query"] == "api-version=2024"
        assert recorded[0]["headers"]["authorization"] == "Bearer REAL"
        assert recorded[0]["body"] == b'{"input":1}'

    def test_rejects_invalid_token_without_forwarding(self):
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://upstream.test",
                StaticHeadersSigning({"x-api-key": "REAL-KEY"}),
            ),
            client=_recording_upstream(recorded),
        )
        with TestClient(app) as client:
            bad = client.post(
                "/v1/messages", headers={"x-api-key": "job-1.deadbeef"}, content=b"{}"
            )
            missing = client.post("/v1/messages", content=b"{}")
        assert bad.status_code == 401
        assert missing.status_code == 401
        assert recorded == []

    def test_refuses_a_run_that_has_ended_without_forwarding(self):
        role = f"agent_{uuid4().hex}"
        placeholder = issue_run_credentials("job-1", role, "discovery").llm_placeholder
        revoke_run_credentials(role)
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            upstream=lambda: LlmUpstream(
                "https://upstream.test",
                StaticHeadersSigning({"x-api-key": "REAL-KEY"}),
            ),
            client=_recording_upstream(recorded),
        )
        with TestClient(app) as client:
            resp = client.post("/v1/messages", headers={"x-api-key": placeholder}, content=b"{}")
        assert resp.status_code == 401
        assert recorded == []

    def test_unsupported_provider_returns_502(self, placeholder_for):
        placeholder = placeholder_for("job-1")

        def _raise() -> LlmUpstream:
            raise ValueError("not supported")

        app = create_llm_proxy_app(
            upstream=_raise,
            client=_recording_upstream([]),
        )
        with TestClient(app) as client:
            resp = client.post("/v1/messages", headers={"x-api-key": placeholder}, content=b"{}")
        assert resp.status_code == 502


class TestVertexProxyForwarding:
    """The proxy signs Vertex calls with the web's credential and forwards them
    to the regional Vertex endpoint; the stub upstream stands in for it."""

    _VERTEX_PATH = (
        "/projects/proj-1/locations/europe-west1/publishers/anthropic/"
        "models/claude-sonnet-4-5@20250929:rawPredict"
    )

    def _app(self, recorded: list[dict], credentials: FakeVertexCredentials) -> Starlette:
        upstream = LlmUpstream(
            "https://us-east5-aiplatform.googleapis.com/v1",
            GoogleAdcSigning(credentials),
        )
        return create_llm_proxy_app(upstream=lambda: upstream, client=_recording_upstream(recorded))

    def test_raw_predict_reaches_the_regional_vertex_endpoint(self, placeholder_for):
        """The container's rawPredict reaches the regional Vertex endpoint with a
        fresh Google bearer, the body unchanged, and the response byte-identical."""
        placeholder = placeholder_for("job-1")
        credentials = FakeVertexCredentials()
        recorded: list[dict] = []
        app = self._app(recorded, credentials)
        with TestClient(app) as client:
            resp = client.post(
                self._VERTEX_PATH,
                headers={
                    "authorization": f"Bearer {placeholder}",
                    "anthropic-version": "vertex-2023-10-16",
                    "content-type": "application/json",
                },
                content=b'{"anthropic_version": "vertex-2023-10-16", "messages": []}',
            )
        assert resp.status_code == 200
        assert resp.content == b"upstream-body"
        assert len(recorded) == 1
        forwarded = recorded[0]
        # Localized to the region the request path names, not the default one.
        assert forwarded["host"] == "europe-west1-aiplatform.googleapis.com"
        assert forwarded["path"] == "/v1" + self._VERTEX_PATH
        assert forwarded["headers"]["authorization"] == "Bearer tok-1"
        assert forwarded["headers"]["anthropic-version"] == "vertex-2023-10-16"
        assert placeholder not in forwarded["headers"].values()
        assert forwarded["body"] == b'{"anthropic_version": "vertex-2023-10-16", "messages": []}'
        # The bearer came from the credential's cached token, no mint needed.
        assert credentials.refreshes == []

    def test_stream_raw_predict_streams_through_byte_identical(self, placeholder_for):
        """Streaming bytes pass through unchanged and the response stays streamed."""
        placeholder = placeholder_for("job-2")
        recorded: list[dict] = []
        app = self._app(recorded, FakeVertexCredentials())
        path = self._VERTEX_PATH[: -len(":rawPredict")] + ":streamRawPredict"
        with TestClient(app) as client:
            with client.stream(
                "POST", path, headers={"authorization": f"Bearer {placeholder}"}, content=b"{}"
            ) as resp:
                chunks = list(resp.iter_raw())
        assert resp.status_code == 200
        assert b"".join(chunks) == b"upstream-body"
        # A streamed response, not a buffered one: no content-length was set.
        assert "content-length" not in resp.headers
        assert resp.headers["content-type"] == "application/json"
        assert recorded[0]["host"] == "europe-west1-aiplatform.googleapis.com"

    def test_the_second_request_reuses_the_minted_token(self, placeholder_for):
        placeholder = placeholder_for("job-3")
        credentials = FakeVertexCredentials()
        recorded: list[dict] = []
        app = self._app(recorded, credentials)
        with TestClient(app) as client:
            for _ in range(2):
                resp = client.post(
                    self._VERTEX_PATH,
                    headers={"authorization": f"Bearer {placeholder}"},
                    content=b"{}",
                )
                assert resp.status_code == 200
        assert [f["headers"]["authorization"] for f in recorded] == ["Bearer tok-1"] * 2
        assert credentials.refreshes == []

    def test_a_wrong_placeholder_is_refused_without_forwarding(self):
        recorded: list[dict] = []
        app = self._app(recorded, FakeVertexCredentials())
        with TestClient(app) as client:
            resp = client.post(
                self._VERTEX_PATH,
                headers={"authorization": f"Bearer job-1.{'0' * 64}"},
                content=b"{}",
            )
        assert resp.status_code == 401
        assert recorded == []


def _recording_bedrock_upstream(recorded: list[dict], payload: bytes) -> httpx.AsyncClient:
    """A stub bedrock-runtime that records the signed request and streams ``payload``
    back in two raw chunks, so the eventstream's byte-identity is observable."""

    async def endpoint(request: Request) -> StreamingResponse:
        body = await request.body()
        recorded.append(
            {
                "method": request.method,
                "host": request.url.hostname,
                "path": request.url.path,
                "query": request.url.query,
                "headers": dict(request.headers),
                "body": body,
            }
        )

        async def stream():
            half = len(payload) // 2
            yield payload[:half]
            yield payload[half:]

        return StreamingResponse(stream(), media_type="application/vnd.amazon.eventstream")

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                endpoint,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ]
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


class TestBedrockSigV4ProxyForwarding:
    """The proxy signs Bedrock SigV4 calls with the web's AWS credential and
    forwards them to the bedrock-runtime endpoint; the stub stands in for it."""

    _PATH = "/model/us.anthropic.claude-sonnet-5-5/invoke-with-response-stream"
    # A binary eventstream-like payload with non-UTF8 bytes, to prove the proxy
    # forwards the eventstream untouched (no re-encoding).
    _EVENTSTREAM = bytes(range(256)) + b"\x00\x01\xfe\xff"

    def _app(self, recorded: list[dict], payload: bytes = b"upstream-body") -> Starlette:
        upstream = LlmUpstream(
            "https://bedrock-runtime.us-east-1.amazonaws.com",
            SigV4Signing(region="us-east-1"),
        )
        return create_llm_proxy_app(
            upstream=lambda: upstream,
            client=_recording_bedrock_upstream(recorded, payload),
        )

    def test_stream_reaches_the_stub_with_a_valid_sigv4(self, placeholder_for, monkeypatch):
        """A streamed InvokeModelWithResponseStream reaches the stub with a valid
        SigV4 signature (verified against the web's credential), the body
        unchanged, and the eventstream response byte-identical."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        placeholder = placeholder_for("job-1")
        recorded: list[dict] = []
        app = self._app(recorded, payload=self._EVENTSTREAM)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                self._PATH,
                headers={
                    "authorization": f"Bearer {placeholder}",
                    "content-type": "application/json",
                },
                content=b'{"model":"m"}',
            ) as resp:
                chunks = list(resp.iter_raw())
        assert resp.status_code == 200
        # The eventstream bytes pass through unchanged, streamed (not buffered):
        # the binary payload is byte-identical and no content-length was set.
        assert b"".join(chunks) == self._EVENTSTREAM
        assert "content-length" not in resp.headers
        assert resp.headers["content-type"] == "application/vnd.amazon.eventstream"
        assert len(recorded) == 1
        forwarded = recorded[0]
        assert forwarded["host"] == "bedrock-runtime.us-east-1.amazonaws.com"
        assert forwarded["path"] == self._PATH
        assert forwarded["body"] == b'{"model":"m"}'
        # The container's placeholder bearer is gone, replaced by a SigV4 signature.
        assert placeholder not in forwarded["headers"].values()
        verify_sigv4(forwarded, access_key="TESTAKID", secret="TESTSECRET", region="us-east-1")

    def test_non_stream_invoke_is_signed_too(self, placeholder_for, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        placeholder = placeholder_for("job-2")
        recorded: list[dict] = []
        app = self._app(recorded)
        with TestClient(app) as client:
            resp = client.post(
                "/model/us.anthropic.claude-sonnet-5-5/invoke",
                headers={"authorization": f"Bearer {placeholder}"},
                content=b'{"model":"m"}',
            )
        assert resp.status_code == 200
        forwarded = recorded[0]
        assert forwarded["host"] == "bedrock-runtime.us-east-1.amazonaws.com"
        verify_sigv4(forwarded, access_key="TESTAKID", secret="TESTSECRET", region="us-east-1")

    def test_a_wrong_placeholder_is_refused_without_forwarding(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "TESTAKID")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "TESTSECRET")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        recorded: list[dict] = []
        app = self._app(recorded)
        with TestClient(app) as client:
            resp = client.post(
                self._PATH,
                headers={"authorization": f"Bearer job-1.{'0' * 64}"},
                content=b"{}",
            )
        assert resp.status_code == 401
        assert recorded == []


class TestStartLlmProxy:
    """start_llm_proxy wires the real proxy app into its listener task."""

    async def test_start_wires_proxy_app_and_starts_once(
        self, active_provider, placeholder_for, monkeypatch
    ) -> None:
        from openscientist import llm_proxy

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="real-key",
            OPENSCIENTIST_LLM_PROXY_PORT=str(port),
        )
        assert provider.airgap_egress().mode is AirgapEgress.PROXY

        configs: list[Config] = []
        served: list[socket.socket] = []

        class _FakeServer:
            def __init__(self, config: Config) -> None:
                configs.append(config)

            async def serve(self, sockets: list[socket.socket]) -> None:
                served.extend(sockets)

        recorded: list[dict] = []

        async def endpoint(request: Request) -> StreamingResponse:
            body = await request.body()
            recorded.append(
                {"path": request.url.path, "headers": dict(request.headers), "body": body}
            )

            async def stream():
                yield b"upstream-body"

            return StreamingResponse(stream())

        upstream_app = Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])
        real_async_client = httpx.AsyncClient

        def make_client(*, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
            # Answer the started app's upstream in-memory, offline.
            return real_async_client(transport=httpx.ASGITransport(app=upstream_app))

        monkeypatch.setattr(llm_proxy, "_NoSignalServer", _FakeServer)
        monkeypatch.setattr(llm_proxy, "_proxy_server", None)
        monkeypatch.setattr(llm_proxy, "_proxy_task", None)
        monkeypatch.setattr(httpx, "AsyncClient", make_client)

        await llm_proxy.start_llm_proxy()
        await llm_proxy.start_llm_proxy()
        await asyncio.sleep(0)

        # One listener on the configured proxy port, holding the proxy app.
        assert len(configs) == 1
        assert llm_proxy._proxy_task is not None
        assert [sock.getsockname()[1] for sock in served] == [port]
        for sock in served:
            sock.close()
        app = cast("ASGIApp", configs[0].app)

        placeholder = placeholder_for("job-1")
        with TestClient(app) as client:
            missing = client.post("/v1/messages", content=b"{}")
            ok = client.post(
                "/v1/messages",
                headers={"x-api-key": placeholder, "anthropic-version": "2023-06-01"},
                content=b'{"m":1}',
            )
        # The wired-up app is the real proxy: a credentialless request is refused,
        # and a live run's credential is substituted with the real key.
        assert missing.status_code == 401
        assert ok.status_code == 200
        assert ok.content == b"upstream-body"
        forwarded = recorded[0]
        assert forwarded["path"] == "/v1/messages"
        assert forwarded["headers"]["x-api-key"] == "real-key"
        assert forwarded["headers"]["anthropic-version"] == "2023-06-01"
        assert forwarded["body"] == b'{"m":1}'

    async def test_the_listener_starts_for_a_provider_without_an_upstream(
        self, active_provider, placeholder_for, monkeypatch
    ) -> None:
        """The proxy listens for every provider; one without an upstream just 502s
        its traffic, so nothing ever bypasses it with a real credential."""
        from openscientist import llm_proxy

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # The ChatGPT-login provider has no API key, hence no upstream.
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="openai",
            CODEX_AUTH_HOST_PATH="/tmp/auth.json",
            OPENSCIENTIST_LLM_PROXY_PORT=str(port),
        )
        assert provider.airgap_egress().mode is AirgapEgress.UNSUPPORTED

        configs: list[Config] = []
        served: list[socket.socket] = []

        class _FakeServer:
            def __init__(self, config: Config) -> None:
                configs.append(config)

            async def serve(self, sockets: list[socket.socket]) -> None:
                served.extend(sockets)

        monkeypatch.setattr(llm_proxy, "_NoSignalServer", _FakeServer)
        monkeypatch.setattr(llm_proxy, "_proxy_server", None)
        monkeypatch.setattr(llm_proxy, "_proxy_task", None)

        await llm_proxy.start_llm_proxy()
        await asyncio.sleep(0)

        assert len(configs) == 1
        assert [sock.getsockname()[1] for sock in served] == [port]
        for sock in served:
            sock.close()
        app = cast("ASGIApp", configs[0].app)

        placeholder = placeholder_for("job-1")
        with TestClient(app) as client:
            resp = client.post("/v1/messages", headers={"x-api-key": placeholder}, content=b"{}")
        assert resp.status_code == 502


class TestRunnerInjection:
    """The runner merges the provider env and injects the per-job secret."""

    def test_build_env_merges_provider_env_and_secret(self):
        settings = SimpleNamespace(
            container=SimpleNamespace(host_project_dir=None),
            provider=SimpleNamespace(google_application_credentials=None),
            database=SimpleNamespace(effective_database_url="postgresql://db"),
            phenix=SimpleNamespace(phenix_host_path=None),
            airgap=SimpleNamespace(enabled=False),
            secret_key="master-key",
        )
        provider_env = {
            "ANTHROPIC_BASE_URL": "http://openscientist:8081",
            "ANTHROPIC_API_KEY": "job-1.tok",
        }
        env = JobContainerRunner._build_container_environment(
            cast(Settings, settings),
            job_id="job-1",
            job_mount="/agent/jobs/job-1",
            provider_env=provider_env,
            database_url="postgresql://agent",
            exec_token="job-1.exec",
            airgapped=False,
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://openscientist:8081"
        assert env["ANTHROPIC_API_KEY"] == "job-1.tok"
        assert env["OPENSCIENTIST_SECRET_KEY"] == derive_job_secret("master-key", "job-1")


class TestAirgapPosture:
    """Each provider reports the air-gap posture its auth shape implies."""

    def test_anthropic_api_key_proxies(self, active_provider):
        p = active_provider(OPENSCIENTIST_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_anthropic_oauth_proxies(self, active_provider):
        p = active_provider(OPENSCIENTIST_PROVIDER="anthropic", CLAUDE_CODE_OAUTH_TOKEN="o")
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_openai_api_key_proxies(self, active_provider):
        p = active_provider(OPENSCIENTIST_PROVIDER="openai", OPENAI_API_KEY="sk")
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_openai_chatgpt_login_unsupported(self, active_provider):
        p = active_provider(OPENSCIENTIST_PROVIDER="openai", CODEX_AUTH_HOST_PATH="/tmp/auth.json")
        posture = p.airgap_egress()
        assert posture.mode is AirgapEgress.UNSUPPORTED
        assert "chatgpt.com" in posture.reason

    def test_azure_proxies(self, active_provider):
        p = active_provider(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_RESOURCE="r",
            OPENSCIENTIST_MODEL="d",
        )
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_cborg_proxies(self, active_provider):
        p = active_provider(
            OPENSCIENTIST_PROVIDER="cborg",
            ANTHROPIC_AUTH_TOKEN="t",
            ANTHROPIC_BASE_URL="https://api.cborg.lbl.gov",
        )
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_foundry_proxies(self, active_provider):
        p = active_provider(
            OPENSCIENTIST_PROVIDER="foundry",
            ANTHROPIC_FOUNDRY_RESOURCE="r",
            ANTHROPIC_FOUNDRY_API_KEY="k",
        )
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_ollama_proxies(self, active_provider):
        p = active_provider(OPENSCIENTIST_PROVIDER="ollama")
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_vllm_proxies(self, active_provider):
        p = active_provider(OPENSCIENTIST_PROVIDER="vllm", OPENSCIENTIST_MODEL="Qwen/Qwen3-32B")
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_llamacpp_proxies(self, active_provider):
        p = active_provider(
            OPENSCIENTIST_PROVIDER="llamacpp",
            OPENSCIENTIST_MODEL="meta-llama/Llama-3.1-8B-Instruct",
        )
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_bedrock_bearer_proxies(self, active_provider):
        p = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="tok",
        )
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_vertex_proxies(self, active_provider, tmp_path):
        creds = tmp_path / "sa.json"
        creds.write_text('{"type": "service_account"}')
        p = active_provider(
            OPENSCIENTIST_PROVIDER="vertex",
            ANTHROPIC_VERTEX_PROJECT_ID="proj-1",
            GOOGLE_APPLICATION_CREDENTIALS=str(creds),
            GCP_BILLING_ACCOUNT_ID="012345-ABCDEF",
            CLOUD_ML_REGION="us-east5",
        )
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    def test_bedrock_sigv4_proxies(self, active_provider):
        # SigV4 routes through the proxy too: the web signs, the container
        # holds no credential, so an air-gapped job reaches nothing but the proxy.
        p = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="ak",
            AWS_SECRET_ACCESS_KEY="sk",
        )
        assert p.airgap_egress().mode is AirgapEgress.PROXY

    @pytest.mark.parametrize(
        "env",
        [
            {"OPENSCIENTIST_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"},
            {"OPENSCIENTIST_PROVIDER": "anthropic", "CLAUDE_CODE_OAUTH_TOKEN": "o"},
            {"OPENSCIENTIST_PROVIDER": "openai", "OPENAI_API_KEY": "sk"},
            {"OPENSCIENTIST_PROVIDER": "ollama"},
            {"OPENSCIENTIST_PROVIDER": "vllm", "OPENSCIENTIST_MODEL": "Qwen/Qwen3-32B"},
            {
                "OPENSCIENTIST_PROVIDER": "vllm",
                "OPENSCIENTIST_MODEL": "Qwen/Qwen3-32B",
                "VLLM_API_KEY": "vk",
            },
            {"OPENSCIENTIST_PROVIDER": "llamacpp", "OPENSCIENTIST_MODEL": "llama-3.1-8b"},
            {
                "OPENSCIENTIST_PROVIDER": "llamacpp",
                "OPENSCIENTIST_MODEL": "llama-3.1-8b",
                "LLAMACPP_API_KEY": "lk",
            },
            {
                "OPENSCIENTIST_PROVIDER": "bedrock",
                "AWS_REGION": "us-east-1",
                "AWS_BEARER_TOKEN_BEDROCK": "tok",
            },
            {
                "OPENSCIENTIST_PROVIDER": "bedrock",
                "AWS_REGION": "us-east-1",
                "AWS_ACCESS_KEY_ID": "ak",
                "AWS_SECRET_ACCESS_KEY": "sk",
            },
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj-1",
                "GOOGLE_APPLICATION_CREDENTIALS": "/dev/null",
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ],
    )
    def test_posture_proxy_iff_proxy_routing(self, active_provider, env):
        """The invariant: PROXY posture exactly when the container is routed at
        the proxy. Guards airgap_egress and proxy_env_overrides against drift."""
        p = active_provider(**env)
        routed = bool(p.proxy_env_overrides(proxy_base_url="http://x", placeholder="y"))
        assert (p.airgap_egress().mode is AirgapEgress.PROXY) == routed


class TestHarnessRouting:
    """A provider-agnostic harness (omp) must be routed at the proxy whenever the
    container is. Where these disagree, omp talks to the vendor directly with the
    real credential and the key-replacement proxy is bypassed."""

    @pytest.mark.parametrize(
        ("env", "expected_key"),
        [
            (
                {"OPENSCIENTIST_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"},
                "ANTHROPIC_BASE_URL",
            ),
            (
                {
                    "OPENSCIENTIST_PROVIDER": "cborg",
                    "ANTHROPIC_AUTH_TOKEN": "t",
                    "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
                },
                "ANTHROPIC_BASE_URL",
            ),
            (
                {
                    "OPENSCIENTIST_PROVIDER": "foundry",
                    "ANTHROPIC_FOUNDRY_RESOURCE": "r",
                    "ANTHROPIC_FOUNDRY_API_KEY": "k",
                },
                # omp reads FOUNDRY_BASE_URL, not the ANTHROPIC_FOUNDRY_BASE_URL
                # the container env uses. Getting this wrong sent every turn to
                # api.anthropic.com, which the airgap firewall drops.
                "FOUNDRY_BASE_URL",
            ),
            ({"OPENSCIENTIST_PROVIDER": "openai", "OPENAI_API_KEY": "sk"}, "OPENAI_BASE_URL"),
        ],
    )
    def test_proxied_provider_routes_the_harness_at_the_proxy(
        self, active_provider, env, expected_key
    ):
        p = active_provider(**env)
        assert p.airgap_egress().mode is AirgapEgress.PROXY, "recipe should be a proxied one"
        assert p.harness_env(proxy="http://openscientist:8081")[expected_key] == (
            "http://openscientist:8081"
        )

    #: Credential env names the shipped omp binary actually reads, from its own
    #: envKeys resolution order: ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_OAUTH_TOKEN
    #: then ANTHROPIC_API_KEY in Foundry mode, the latter two otherwise, plus
    #: OPENAI_API_KEY for the OpenAI family. Notably absent are the Claude Code
    #: names ANTHROPIC_AUTH_TOKEN and CLAUDE_CODE_OAUTH_TOKEN, which appear zero
    #: times in the binary.
    _OMP_CREDENTIAL_NAMES = frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_OAUTH_TOKEN",
            "ANTHROPIC_FOUNDRY_API_KEY",
            "OPENAI_API_KEY",
        }
    )

    @pytest.mark.parametrize(
        "env",
        [
            {"OPENSCIENTIST_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"},
            {
                "OPENSCIENTIST_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "t",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
            {
                "OPENSCIENTIST_PROVIDER": "foundry",
                "ANTHROPIC_FOUNDRY_RESOURCE": "r",
                "ANTHROPIC_FOUNDRY_API_KEY": "k",
            },
            {"OPENSCIENTIST_PROVIDER": "openai", "OPENAI_API_KEY": "sk"},
            # Self-hosted providers route themselves rather than inheriting the
            # OpenAI default, so their credential wiring is bespoke and is the
            # class most likely to omit one.
            {"OPENSCIENTIST_PROVIDER": "vllm", "OPENSCIENTIST_MODEL": "Qwen/Qwen3-32B"},
            {
                "OPENSCIENTIST_PROVIDER": "vllm",
                "OPENSCIENTIST_MODEL": "Qwen/Qwen3-32B",
                "VLLM_API_KEY": "vk",
            },
            {"OPENSCIENTIST_PROVIDER": "llamacpp", "OPENSCIENTIST_MODEL": "llama-3.1-8b"},
            {
                "OPENSCIENTIST_PROVIDER": "llamacpp",
                "OPENSCIENTIST_MODEL": "llama-3.1-8b",
                "LLAMACPP_API_KEY": "lk",
            },
        ],
    )
    def test_proxied_provider_gives_the_harness_a_credential_it_reads(self, active_provider, env):
        """Routing omp at the proxy is only half of reachability: it also has to
        arrive with a credential. CBORG published its bearer solely as
        ANTHROPIC_AUTH_TOKEN, which omp never reads, so every turn hit the proxy
        unauthenticated. Checking the base URL alone did not catch that.
        """
        proxy = "http://openscientist:8081"
        p = active_provider(**env)
        published = set(p.proxy_env_overrides(proxy_base_url=proxy, placeholder="ph"))
        published |= set(p.harness_env(proxy=proxy))
        assert published & self._OMP_CREDENTIAL_NAMES, (
            f"{env['OPENSCIENTIST_PROVIDER']} hands omp no credential it reads, so the "
            f"turn reaches the proxy unauthenticated. Published: {sorted(published)}"
        )

    def test_vertex_routes_the_harness_with_the_run_placeholder(
        self, active_provider, tmp_path, monkeypatch
    ):
        """omp signs Vertex calls with ``GOOGLE_CLOUD_ACCESS_TOKEN`` before it
        would touch the default credentials, so the run's placeholder there is
        exchanged by the proxy, and the harness never gets a GCP credential."""
        creds = tmp_path / "sa.json"
        creds.write_text('{"type": "service_account"}')
        p = active_provider(
            OPENSCIENTIST_PROVIDER="vertex",
            ANTHROPIC_VERTEX_PROJECT_ID="proj-1",
            GOOGLE_APPLICATION_CREDENTIALS=str(creds),
            GCP_BILLING_ACCOUNT_ID="012345-ABCDEF",
            CLOUD_ML_REGION="us-east5",
        )
        # Published by the container env once the proxy override applies.
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "job-1.tok")
        env = p.harness_env(proxy="http://openscientist:8081")
        assert env == {"GOOGLE_CLOUD_ACCESS_TOKEN": "job-1.tok"}
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
        assert str(creds) not in env.values()

    def test_vertex_refuses_to_launch_without_the_proxy(self, active_provider, tmp_path):
        """Without the proxy omp would reach Vertex directly, so there is no
        harness route and the launch fails fast."""
        creds = tmp_path / "sa.json"
        creds.write_text('{"type": "service_account"}')
        p = active_provider(
            OPENSCIENTIST_PROVIDER="vertex",
            ANTHROPIC_VERTEX_PROJECT_ID="proj-1",
            GOOGLE_APPLICATION_CREDENTIALS=str(creds),
            GCP_BILLING_ACCOUNT_ID="012345-ABCDEF",
            CLOUD_ML_REGION="us-east5",
        )
        with pytest.raises(ValueError, match="proxy"):
            p.harness_env(proxy=None)

    def test_bedrock_sigv4_refuses_the_omp_harness(self, active_provider):
        """omp signs Bedrock with SigV4 and exposes no proxy endpoint override,
        so the proxy cannot verify its request. Refuse rather than let omp reach
        AWS directly with the web's real credential."""
        p = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="ak",
            AWS_SECRET_ACCESS_KEY="sk",
        )
        with pytest.raises(ValueError, match="omp harness"):
            p.harness_env(proxy="http://openscientist:8081")

    def test_bedrock_bearer_refuses_the_omp_harness(self, active_provider):
        """Same for bearer: there is no safe omp route, so refuse and fail fast
        at launch, with no direct egress and no in-container credential."""
        p = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="tok",
        )
        with pytest.raises(ValueError, match="omp harness"):
            p.harness_env(proxy="http://openscientist:8081")

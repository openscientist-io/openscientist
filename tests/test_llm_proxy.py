"""Tests for the LLM key-replacement proxy (Claude backend)."""

import hashlib
import hmac
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_container.secrets import (
    derive_job_secret,
    derive_llm_proxy_token,
    make_job_placeholder,
    verify_job_placeholder,
)
from openscientist.llm_proxy import container_proxy_base_url, create_llm_proxy_app
from openscientist.providers import get_provider
from openscientist.providers.base import AirgapEgress, LlmUpstream, Provider
from openscientist.settings import Settings, get_settings


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
        "AZURE_OPENAI_DEPLOYMENT",
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
    )

    def _select(**env: str) -> Provider:
        for key in reset:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", env.pop("OPENSCIENTIST_PROVIDER"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_provider()

    yield _select
    get_settings.cache_clear()


class TestProxyToken:
    """Per-job proxy token and placeholder derivation."""

    def test_token_matches_reference_and_is_distinct(self):
        reference = hmac.new(b"master", b"llm_proxy:job-1", hashlib.sha256).hexdigest()
        assert derive_llm_proxy_token("master", "job-1") == reference
        assert len(reference) == 64
        # Distinct label from the settings secret, so one does not reveal the other.
        assert derive_llm_proxy_token("master", "job-1") != derive_job_secret("master", "job-1")
        assert derive_llm_proxy_token("master", "job-1") != "master"

    def test_placeholder_round_trip(self):
        placeholder = make_job_placeholder("master", "job-1")
        assert placeholder == "job-1." + derive_llm_proxy_token("master", "job-1")
        assert verify_job_placeholder("master", placeholder)

    def test_placeholder_rejects_tampering_and_wrong_master(self):
        placeholder = make_job_placeholder("master", "job-1")
        flipped = placeholder[:-1] + ("0" if placeholder[-1] != "0" else "1")
        assert not verify_job_placeholder("master", flipped)
        assert not verify_job_placeholder("other-master", placeholder)
        assert not verify_job_placeholder("master", "no-separator")
        assert not verify_job_placeholder("master", ".onlytoken")
        assert not verify_job_placeholder("master", "jobonly.")

    def test_placeholder_tolerates_job_id_with_separator(self):
        placeholder = make_job_placeholder("master", "job.with.dots")
        assert verify_job_placeholder("master", placeholder)


class TestClaudeUpstream:
    """Real upstream and auth-header derivation, on the Provider hierarchy."""

    def test_anthropic_upstream(self, active_provider):
        provider = active_provider(OPENSCIENTIST_PROVIDER="anthropic", ANTHROPIC_API_KEY="real-key")
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.anthropic.com", {"x-api-key": "real-key"}
        )

    def test_cborg_upstream_strips_trailing_slash(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="cborg",
            ANTHROPIC_AUTH_TOKEN="real-tok",
            ANTHROPIC_BASE_URL="https://api.cborg.lbl.gov/",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.cborg.lbl.gov", {"authorization": "Bearer real-tok"}
        )

    def test_foundry_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="foundry",
            ANTHROPIC_FOUNDRY_RESOURCE="myfoundry",
            ANTHROPIC_FOUNDRY_API_KEY="fkey",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://myfoundry.services.ai.azure.com/anthropic", {"x-api-key": "fkey"}
        )

    def test_anthropic_oauth_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="anthropic", CLAUDE_CODE_OAUTH_TOKEN="oauth-real"
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.anthropic.com", {"authorization": "Bearer oauth-real"}
        )

    def test_bedrock_bearer_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="bt-real",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://bedrock-runtime.us-east-1.amazonaws.com",
            {"authorization": "Bearer bt-real"},
        )


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


class TestCodexUpstream:
    """Codex backend upstream derivation on the Provider hierarchy."""

    def test_openai_upstream(self, active_provider):
        provider = active_provider(OPENSCIENTIST_PROVIDER="openai", OPENAI_API_KEY="sk-real")
        assert provider.llm_upstream() == LlmUpstream(
            "https://api.openai.com/v1", {"authorization": "Bearer sk-real"}
        )

    def test_azure_upstream(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="az-real",
            AZURE_OPENAI_RESOURCE="myres",
            AZURE_OPENAI_DEPLOYMENT="gpt5",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "https://myres.openai.azure.com/openai/v1", {"authorization": "Bearer az-real"}
        )

    def test_ollama_upstream_is_keyless(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="ollama", OLLAMA_BASE_URL="http://ollama:11434/v1"
        )
        assert provider.llm_upstream() == LlmUpstream("http://ollama:11434/v1", {})

    def test_vllm_upstream_is_keyless_without_api_key(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="vllm",
            VLLM_BASE_URL="http://vllm:8000/v1",
            OPENSCIENTIST_MODEL="Qwen/Qwen3-32B",
        )
        assert provider.llm_upstream() == LlmUpstream("http://vllm:8000/v1", {})

    def test_vllm_upstream_injects_bearer_when_keyed(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="vllm",
            VLLM_BASE_URL="http://vllm:8000/v1",
            OPENSCIENTIST_MODEL="Qwen/Qwen3-32B",
            VLLM_API_KEY="vk-real",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "http://vllm:8000/v1", {"authorization": "Bearer vk-real"}
        )

    def test_llamacpp_upstream_is_keyless_without_api_key(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="llamacpp",
            LLAMACPP_BASE_URL="http://llamacpp:8080/v1",
            OPENSCIENTIST_MODEL="meta-llama/Llama-3.1-8B-Instruct",
        )
        assert provider.llm_upstream() == LlmUpstream("http://llamacpp:8080/v1", {})

    def test_llamacpp_upstream_injects_bearer_when_keyed(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="llamacpp",
            LLAMACPP_BASE_URL="http://llamacpp:8080/v1",
            OPENSCIENTIST_MODEL="meta-llama/Llama-3.1-8B-Instruct",
            LLAMACPP_API_KEY="lk-real",
        )
        assert provider.llm_upstream() == LlmUpstream(
            "http://llamacpp:8080/v1", {"authorization": "Bearer lk-real"}
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
            AZURE_OPENAI_DEPLOYMENT="gpt5",
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
            AZURE_OPENAI_DEPLOYMENT="gpt5",
            OPENSCIENTIST_LLM_PROXY_URL="http://openscientist:8081",
        )
        assert 'base_url = "http://openscientist:8081"' in provider.codex_config_overrides()

    def test_azure_config_uses_real_base_without_proxy(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="az-real",
            AZURE_OPENAI_RESOURCE="myres",
            AZURE_OPENAI_DEPLOYMENT="gpt5",
        )
        toml = provider.codex_config_overrides()
        assert 'base_url = "https://myres.openai.azure.com/openai/v1"' in toml

    def test_ollama_config_points_at_proxy(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://ollama:11434/v1",
            OPENSCIENTIST_LLM_PROXY_URL="http://openscientist:8081",
        )
        toml = provider.codex_config_overrides()
        assert 'base_url = "http://openscientist:8081"' in toml
        assert "requires_openai_auth = true" in toml
        assert 'env_key = "OPENAI_API_KEY"' in toml

    def test_ollama_config_keyless_without_proxy(self, active_provider):
        provider = active_provider(
            OPENSCIENTIST_PROVIDER="ollama", OLLAMA_BASE_URL="http://ollama:11434/v1"
        )
        toml = provider.codex_config_overrides()
        assert 'base_url = "http://ollama:11434/v1"' in toml
        assert "requires_openai_auth = false" in toml


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

    def test_substitutes_key_and_preserves_request(self):
        master = "master-key"
        placeholder = make_job_placeholder(master, "job-1")
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            master_key=lambda: master,
            upstream=lambda: LlmUpstream("https://upstream.test", {"x-api-key": "REAL-KEY"}),
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

    def test_accepts_bearer_and_sets_authorization(self):
        master = "master-key"
        placeholder = make_job_placeholder(master, "job-x")
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            master_key=lambda: master,
            upstream=lambda: LlmUpstream(
                "https://cborg.test", {"authorization": "Bearer REAL-TOK"}
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

    def test_forwards_responses_wire_with_query(self):
        master = "master-key"
        placeholder = make_job_placeholder(master, "job-c")
        recorded: list[dict] = []
        app = create_llm_proxy_app(
            master_key=lambda: master,
            upstream=lambda: LlmUpstream(
                "https://api.openai.com/v1", {"authorization": "Bearer REAL"}
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
            master_key=lambda: "master-key",
            upstream=lambda: LlmUpstream("https://upstream.test", {"x-api-key": "REAL-KEY"}),
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

    def test_unsupported_provider_returns_502(self):
        master = "master-key"
        placeholder = make_job_placeholder(master, "job-1")

        def _raise() -> LlmUpstream:
            raise ValueError("not supported")

        app = create_llm_proxy_app(
            master_key=lambda: master,
            upstream=_raise,
            client=_recording_upstream([]),
        )
        with TestClient(app) as client:
            resp = client.post("/v1/messages", headers={"x-api-key": placeholder}, content=b"{}")
        assert resp.status_code == 502


class TestRunnerInjection:
    """The runner merges the provider env and injects the per-job secret."""

    def test_build_env_merges_provider_env_and_secret(self):
        settings = SimpleNamespace(
            container=SimpleNamespace(host_project_dir=None),
            provider=SimpleNamespace(google_application_credentials=None),
            database=SimpleNamespace(
                effective_database_url="postgresql://db",
                effective_admin_database_url="postgresql://admin-db",
            ),
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
            AZURE_OPENAI_DEPLOYMENT="d",
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

    def test_bedrock_sigv4_is_direct(self, active_provider):
        p = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="ak",
            AWS_SECRET_ACCESS_KEY="sk",
        )
        posture = p.airgap_egress()
        assert posture.mode is AirgapEgress.DIRECT
        assert ("bedrock-runtime.us-east-1.amazonaws.com", 443) in posture.direct_endpoints

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

    def test_vertex_routes_the_harness_with_google_names(self, active_provider, tmp_path):
        """Vertex signs its own requests, so it is direct. omp reads the standard
        Google names rather than the Claude Code ones we publish, so without the
        translation omp falls back to the public Anthropic endpoint."""
        creds = tmp_path / "sa.json"
        creds.write_text('{"type": "service_account"}')
        p = active_provider(
            OPENSCIENTIST_PROVIDER="vertex",
            ANTHROPIC_VERTEX_PROJECT_ID="proj-1",
            GOOGLE_APPLICATION_CREDENTIALS=str(creds),
            GCP_BILLING_ACCOUNT_ID="012345-ABCDEF",
            CLOUD_ML_REGION="us-east5",
        )
        env = p.harness_env(proxy=None)
        assert env["GOOGLE_CLOUD_PROJECT"] == "proj-1"
        assert env["GOOGLE_CLOUD_LOCATION"] == "us-east5"
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == str(creds)

    def test_bedrock_sigv4_needs_no_harness_routing(self, active_provider):
        """SigV4 signs its own requests and omp uses the same AWS names we do."""
        p = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="ak",
            AWS_SECRET_ACCESS_KEY="sk",
        )
        assert p.harness_env(proxy=None) == {}

    def test_bedrock_bearer_refuses_to_be_proxied_under_a_generic_harness(self, active_provider):
        """omp exposes no Bedrock base-URL override, so a proxied bearer setup
        cannot be expressed. Refusing beats reaching AWS with the real token."""
        p = active_provider(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_BEARER_TOKEN_BEDROCK="tok",
        )
        with pytest.raises(ValueError, match="cannot be routed through the LLM proxy"):
            p.harness_env(proxy="http://openscientist:8081")

"""Tests for `OllamaClaudeProvider` (local, keyless ClaudeCompatible provider)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.factory import agent_class_for_provider_id
from openscientist.providers import provider_class, provider_ids
from openscientist.providers.base import AirgapEgress, ClaudeCompatible
from openscientist.providers.ollama_claude import KEYLESS_PLACEHOLDER, OllamaClaudeProvider


def _settings(
    *,
    base_url: str = "http://localhost:11434/v1",
    model_default: str = "gpt-oss:20b",
    model: str | None = None,
    anthropic_base_url: str | None = None,
    anthropic_api_key: str | None = None,
    model_context_tokens: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        provider=SimpleNamespace(
            ollama_base_url=base_url,
            ollama_model=model_default,
            model=model,
            anthropic_base_url=anthropic_base_url,
            anthropic_api_key=anthropic_api_key,
            model_context_tokens=model_context_tokens,
        )
    )


def _provider(**kwargs) -> OllamaClaudeProvider:
    with patch(
        "openscientist.providers.ollama_claude.get_settings", return_value=_settings(**kwargs)
    ):
        return OllamaClaudeProvider()


# --- registration + agent family dispatch --------------------------------------


def test_registered_under_ollama_claude_id() -> None:
    assert "ollama-claude" in provider_ids()
    assert provider_class("ollama-claude") is OllamaClaudeProvider


def test_drives_the_claude_agent_not_codex() -> None:
    """The whole point: same server as `ollama`, but the Claude harness."""
    assert agent_class_for_provider_id("ollama-claude") is ClaudeCodeAgent


def test_is_claude_compatible() -> None:
    assert isinstance(_provider(), ClaudeCompatible)


def test_needs_no_operator_config() -> None:
    assert _provider().validate_required_config() == []


# --- base URL shape (the /v1 vs root trap) -------------------------------------


def test_claude_sdk_env_points_at_the_ollama_root_not_the_v1_endpoint() -> None:
    """The Anthropic SDK appends /v1/messages itself, so the base must be the root."""
    p = _provider()
    with patch(
        "openscientist.providers.ollama_claude.get_settings",
        return_value=_settings(base_url="http://host.docker.internal:11434/v1"),
    ):
        env = p.claude_sdk_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:11434"


def test_llm_upstream_is_the_root_so_the_proxy_does_not_double_the_v1() -> None:
    """The proxy appends the request path, which already carries /v1/messages."""
    p = _provider()
    with patch(
        "openscientist.providers.ollama_claude.get_settings",
        return_value=_settings(base_url="http://host:11434/v1"),
    ):
        upstream = p.llm_upstream()
    assert upstream is not None
    assert upstream.base_url == "http://host:11434"
    assert upstream.auth_headers == {}


def test_llm_upstream_ignores_a_proxy_valued_anthropic_base_url() -> None:
    """Resolved host-side to target the proxy, so it must read OLLAMA_BASE_URL."""
    p = _provider()
    with patch(
        "openscientist.providers.ollama_claude.get_settings",
        return_value=_settings(
            base_url="http://host:11434/v1", anthropic_base_url="http://proxy:9099"
        ),
    ):
        upstream = p.llm_upstream()
    assert upstream is not None
    assert upstream.base_url == "http://host:11434"


# --- keyless auth + proxy redirection ------------------------------------------


def test_supplies_a_placeholder_credential_when_none_is_configured() -> None:
    """Ollama ignores it; the Claude CLI refuses to start without one."""
    p = _provider()
    with patch("openscientist.providers.ollama_claude.get_settings", return_value=_settings()):
        env = p.claude_sdk_env()
    assert env["ANTHROPIC_API_KEY"] == KEYLESS_PLACEHOLDER


def test_proxy_env_overrides_redirect_the_cli_and_carry_the_placeholder() -> None:
    p = _provider()
    overrides = p.proxy_env_overrides(proxy_base_url="http://proxy:9099", placeholder="tok-123")
    assert overrides == {
        "ANTHROPIC_BASE_URL": "http://proxy:9099",
        "ANTHROPIC_API_KEY": "tok-123",
    }


def test_in_container_the_proxy_url_wins_over_the_direct_ollama_address() -> None:
    """Round-trip: proxy_env_overrides sets the container env, settings read it
    back there, and claude_sdk_env must not clobber it with Ollama's address."""
    p = _provider()
    with patch(
        "openscientist.providers.ollama_claude.get_settings",
        return_value=_settings(anthropic_base_url="http://proxy:9099", anthropic_api_key="tok-123"),
    ):
        env = p.claude_sdk_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://proxy:9099"
    assert env["ANTHROPIC_API_KEY"] == "tok-123"


# --- air-gap posture -----------------------------------------------------------


def test_airgap_posture_is_proxy_so_the_container_never_learns_the_ollama_address() -> None:
    p = _provider()
    with patch("openscientist.providers.ollama_claude.get_settings", return_value=_settings()):
        posture = p.airgap_egress()
    assert posture.mode is AirgapEgress.PROXY


# --- model selection -----------------------------------------------------------


def test_model_defaults_to_the_configured_ollama_model() -> None:
    p = _provider()
    with patch(
        "openscientist.providers.ollama_claude.get_settings",
        return_value=_settings(model_default="qwen3.6:35b-a3b"),
    ):
        assert p.claude_model_name() == "qwen3.6:35b-a3b"


def test_openscientist_model_overrides_the_ollama_model() -> None:
    p = _provider()
    with patch(
        "openscientist.providers.ollama_claude.get_settings",
        return_value=_settings(model_default="gpt-oss:20b", model="qwen3.6:27b"),
    ):
        assert p.claude_model_name() == "qwen3.6:27b"


def test_model_profile_honours_the_context_override_without_probing() -> None:
    p = _provider()
    settings = _settings(model_default="qwen3.6:35b-a3b", model_context_tokens=32768)
    with (
        patch("openscientist.providers.ollama_claude.get_settings", return_value=settings),
        patch("openscientist.providers._ollama_common.get_settings", return_value=settings),
        patch("openscientist.providers._ollama_common.probe_ollama_context_tokens") as probe,
    ):
        profile = p.model_profile()
    assert profile.id == "qwen3.6:35b-a3b"
    assert profile.context_window_tokens == 32768
    probe.assert_not_called()


# --- environment hygiene -------------------------------------------------------


def test_setup_environment_clears_a_leftover_cborg_token(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-cborg-token")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    p = _provider()
    with patch("openscientist.providers.ollama_claude.get_settings", return_value=_settings()):
        p.setup_environment()
    import os

    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    assert "CLAUDE_CODE_USE_VERTEX" not in os.environ


# --- thinking-block incompatibility --------------------------------------------


def test_upstream_disables_thinking_so_the_sdk_can_parse_the_reply() -> None:
    """Ollama emits `thinking` blocks with no `signature`, which the Claude SDK
    parser rejects outright ("Missing required field ... 'signature'")."""
    p = _provider()
    with patch("openscientist.providers.ollama_claude.get_settings", return_value=_settings()):
        upstream = p.llm_upstream()
    assert upstream is not None
    assert upstream.request_overrides == {"thinking": {"type": "disabled"}}

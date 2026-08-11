"""Shared contract for self-hosted OpenAI-wire providers, parametrized over vLLM and llama.cpp.

Each provider's probe, the one real difference, lives in its own file.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openscientist import models
from openscientist.providers.base import (
    LLM_PROXY_URL_ENV,
    CodexCompatible,
    LlmUpstream,
    OpenAiWireCompatible,
    SelfHostedOpenAiWire,
)
from openscientist.providers.llamacpp import LlamaCppProvider
from openscientist.providers.vllm import VllmProvider

_BASE_SETTINGS_PATH = "openscientist.providers.base.get_settings"
_UNSET = object()


@dataclass(frozen=True)
class Spec:
    """Everything the shared contract needs to exercise one provider."""

    cls: type[SelfHostedOpenAiWire]
    module: str
    id: str
    display_name: str
    server_name: str
    base_url_env: str
    api_key_env: str
    default_base_url: str
    sample_model: str
    base_url_attr: str
    api_key_attr: str

    def settings(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: object = _UNSET,
        model_context_tokens: int | None = None,
    ) -> SimpleNamespace:
        provider = SimpleNamespace(
            model=self.sample_model if model is _UNSET else model,
            model_context_tokens=model_context_tokens,
        )
        setattr(provider, self.base_url_attr, base_url or self.default_base_url)
        setattr(provider, self.api_key_attr, api_key)
        return SimpleNamespace(provider=provider)


SPECS = [
    Spec(
        cls=VllmProvider,
        module="openscientist.providers.vllm",
        id="vllm",
        display_name="vLLM (self-hosted)",
        server_name="vLLM",
        base_url_env="VLLM_BASE_URL",
        api_key_env="VLLM_API_KEY",
        default_base_url="http://localhost:8000/v1",
        sample_model="Qwen/Qwen3-32B",
        base_url_attr="vllm_base_url",
        api_key_attr="vllm_api_key",
    ),
    Spec(
        cls=LlamaCppProvider,
        module="openscientist.providers.llamacpp",
        id="llamacpp",
        display_name="llama.cpp (self-hosted)",
        server_name="llama.cpp",
        base_url_env="LLAMACPP_BASE_URL",
        api_key_env="LLAMACPP_API_KEY",
        default_base_url="http://localhost:8080/v1",
        sample_model="meta-llama/Llama-3.1-8B-Instruct",
        base_url_attr="llamacpp_base_url",
        api_key_attr="llamacpp_api_key",
    ),
]

pytestmark = pytest.mark.parametrize("spec", SPECS, ids=[s.id for s in SPECS])


@contextmanager
def _provider(
    spec: Spec, settings: SimpleNamespace | None = None
) -> Iterator[SelfHostedOpenAiWire]:
    """A constructed provider whose settings the base layer reads."""
    with patch(_BASE_SETTINGS_PATH, return_value=settings or spec.settings()):
        yield spec.cls()


# --- family and identity --------------------------------------------------------


def test_speaks_the_openai_wire_but_is_not_a_codex_backend(spec: Spec) -> None:
    """Implements the OpenAI wire without claiming the Codex contract."""
    with _provider(spec) as p:
        assert isinstance(p, SelfHostedOpenAiWire)
        assert isinstance(p, OpenAiWireCompatible)
        assert not isinstance(p, CodexCompatible)


def test_identity(spec: Spec) -> None:
    with _provider(spec) as p:
        assert p.id == spec.id
        assert p.display_name == spec.display_name
        assert p.server_name == spec.server_name


def test_model_name_comes_from_openscientist_model(spec: Spec) -> None:
    with _provider(spec) as p:
        assert p.effective_model_name() == spec.sample_model


# --- auth surface (keyless / keyed) ---------------------------------------------


def test_llm_upstream_is_keyless_without_api_key(spec: Spec) -> None:
    with _provider(spec) as p:
        assert p.llm_upstream() == LlmUpstream(spec.default_base_url, {})


def test_llm_upstream_injects_bearer_when_keyed(spec: Spec) -> None:
    with _provider(spec, spec.settings(api_key="k-secret")) as p:
        assert p.llm_upstream() == LlmUpstream(
            spec.default_base_url, {"authorization": "Bearer k-secret"}
        )


def test_proxy_env_overrides_keyless_leaves_provider_key_unset(spec: Spec) -> None:
    with _provider(spec) as p:
        env = p.proxy_env_overrides(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
    assert env == {"OPENAI_API_KEY": "job-1.tok", LLM_PROXY_URL_ENV: "http://openscientist:8081"}
    assert spec.api_key_env not in env


def test_proxy_env_overrides_replace_the_real_api_key(spec: Spec) -> None:
    with _provider(spec, spec.settings(api_key="k-secret")) as p:
        env = p.proxy_env_overrides(
            proxy_base_url="http://openscientist:8081", placeholder="job-1.tok"
        )
    # The real key must never reach the job container: the proxy holds it.
    assert env[spec.api_key_env] == "job-1.tok"
    assert "k-secret" not in env.values()


def test_container_env_omits_api_key_when_unset(spec: Spec) -> None:
    # The model is absent on purpose: OPENSCIENTIST_MODEL is forwarded generically.
    env = spec.cls.container_env(spec.settings().provider)
    assert env == {spec.base_url_env: spec.default_base_url}


def test_container_env_carries_api_key_when_set(spec: Spec) -> None:
    env = spec.cls.container_env(spec.settings(api_key="k-secret").provider)
    assert env[spec.api_key_env] == "k-secret"


# --- harness env ----------------------------------------------------------------


def test_harness_env_points_at_server_without_proxy(spec: Spec) -> None:
    with _provider(spec) as p:
        env = p.harness_env(proxy=None)
    # OpenAI clients reject an empty key, so a keyless server gets the id as a dummy.
    assert env == {"OPENAI_BASE_URL": spec.default_base_url, "OPENAI_API_KEY": spec.id}


def test_harness_env_uses_api_key_without_proxy(spec: Spec) -> None:
    with _provider(spec, spec.settings(api_key="k-secret")) as p:
        env = p.harness_env(proxy=None)
    assert env == {"OPENAI_BASE_URL": spec.default_base_url, "OPENAI_API_KEY": "k-secret"}


def test_harness_env_points_at_proxy_when_active(spec: Spec) -> None:
    with _provider(spec) as p:
        env = p.harness_env(proxy="http://openscientist:8081")
    assert env == {"OPENAI_BASE_URL": "http://openscientist:8081"}


# --- required config ------------------------------------------------------------


def test_required_config_errors_demand_a_model(spec: Spec) -> None:
    errors = spec.cls.required_config_errors(spec.settings(model=None).provider)
    assert len(errors) == 1
    assert "OPENSCIENTIST_MODEL" in errors[0]
    assert spec.server_name in errors[0]


def test_validate_required_config_accepts_openscientist_model(spec: Spec) -> None:
    with _provider(spec) as p:
        assert p.validate_required_config() == []


def test_construction_fails_without_a_model(spec: Spec) -> None:
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings(model=None)),
        pytest.raises(ValueError, match="OPENSCIENTIST_MODEL"),
    ):
        spec.cls()


def test_get_cost_info_reports_zero_self_hosted_spend(spec: Spec) -> None:
    with _provider(spec) as p:
        info = p.get_cost_info()
    assert info.total_spend_usd == 0.0
    assert info.recent_spend_usd == 0.0


# --- factory resolution (real settings) -----------------------------------------


def test_get_provider_selects_the_provider(spec: Spec, monkeypatch: pytest.MonkeyPatch) -> None:
    from openscientist.providers import get_provider
    from openscientist.settings import clear_settings_cache

    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", spec.id)
    monkeypatch.setenv("OPENSCIENTIST_MODEL", spec.sample_model)
    clear_settings_cache()
    try:
        assert isinstance(get_provider(), spec.cls)
    finally:
        clear_settings_cache()


def test_missing_model_surfaces_as_a_config_error(
    spec: Spec, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Requiring a served model must reach the operator, not crash the app."""
    from openscientist.providers import check_provider_config
    from openscientist.settings import clear_settings_cache

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", spec.id)
    monkeypatch.delenv("OPENSCIENTIST_MODEL", raising=False)
    clear_settings_cache()
    try:
        configured, name, errors = check_provider_config()
    finally:
        clear_settings_cache()
    assert configured is False
    assert name == spec.id
    assert any("OPENSCIENTIST_MODEL" in e for e in errors)


# --- omp model catalog ----------------------------------------------------------


def test_omp_catalog_declares_the_served_model(spec: Spec, monkeypatch: pytest.MonkeyPatch) -> None:
    """omp resolves --model against this, so id and contextWindow must be real."""
    monkeypatch.delenv(LLM_PROXY_URL_ENV, raising=False)
    with _provider(spec, spec.settings(model_context_tokens=262144)) as p:
        catalog = p.omp_model_catalog(context_window=262144)
    assert catalog is not None
    entry = catalog["providers"][spec.id]
    assert entry["baseUrl"] == spec.default_base_url
    assert entry["api"] == "openai-completions"
    # omp's schema accepts only apiKey, none or oauth.
    assert entry["auth"] == "none"
    assert "apiKey" not in entry
    model = entry["models"][0]
    assert model["id"] == spec.sample_model
    assert model["contextWindow"] == 262144


def test_omp_catalog_carries_the_api_key_when_keyed(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LLM_PROXY_URL_ENV, raising=False)
    with _provider(spec, spec.settings(api_key="k-secret", model_context_tokens=4096)) as p:
        catalog = p.omp_model_catalog(context_window=4096)
    assert catalog is not None
    entry = catalog["providers"][spec.id]
    assert entry["auth"] == "apiKey"
    assert entry["apiKey"] == "k-secret"


def test_omp_catalog_points_at_the_proxy_with_the_placeholder(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the proxy omp must reach the proxy and authenticate as the job."""
    monkeypatch.setenv(LLM_PROXY_URL_ENV, "http://openscientist:8081")
    monkeypatch.setenv("OPENAI_API_KEY", "job-1.tok")
    with _provider(spec, spec.settings(api_key="k-secret", model_context_tokens=4096)) as p:
        catalog = p.omp_model_catalog(context_window=4096)
    assert catalog is not None
    entry = catalog["providers"][spec.id]
    assert entry["baseUrl"] == "http://openscientist:8081"
    assert entry["apiKey"] == "job-1.tok"
    # The real server key stays web-side with the proxy.
    assert "k-secret" not in str(catalog)


# --- model_profile (override / live probe / failure) ----------------------------


def test_model_profile_override_wins_without_probing(spec: Spec) -> None:
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings(model_context_tokens=65536)),
        patch.object(spec.cls, "_probe_context_tokens") as probe,
    ):
        profile = spec.cls().model_profile()
    assert profile.context_window_tokens == 65536
    probe.assert_not_called()


def test_model_profile_uses_live_probe(spec: Spec, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LLM_PROXY_URL_ENV, raising=False)
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings()),
        patch.object(spec.cls, "_probe_context_tokens", return_value=40960) as probe,
    ):
        profile = spec.cls().model_profile()
    assert profile.id == spec.sample_model
    assert profile.context_window_tokens == 40960
    probe.assert_called_once_with(spec.default_base_url, spec.sample_model, None)


def test_model_profile_probes_through_the_proxy_when_air_gapped(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxied fallback probe targets the proxy with the job placeholder."""
    monkeypatch.setenv(LLM_PROXY_URL_ENV, "http://openscientist:8081")
    monkeypatch.setenv("OPENAI_API_KEY", "job-1.tok")
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings(api_key="k-secret")),
        patch.object(spec.cls, "_probe_context_tokens", return_value=4096) as probe,
    ):
        spec.cls().model_profile()
    probe.assert_called_once_with("http://openscientist:8081", spec.sample_model, "job-1.tok")


def test_model_profile_probe_failure_logs_warning_and_defaults(
    spec: Spec, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.delenv(LLM_PROXY_URL_ENV, raising=False)
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings()),
        patch.object(spec.cls, "_probe_context_tokens", return_value=None),
        caplog.at_level(logging.WARNING, logger=spec.module),
    ):
        profile = spec.cls().model_profile()
    assert profile.context_window_tokens == models._DEFAULT_CONTEXT_TOKENS
    # The warning names the server so an operator knows which one failed to answer.
    assert any(
        f"Could not probe the {spec.server_name} context window" in r.message
        for r in caplog.records
    )


# --- app-side window resolution (probe_context_window / prelaunch env) -----------


def test_probe_context_window_hits_the_direct_server(spec: Spec) -> None:
    """App-side resolution must hit the real server directly, not the proxy."""
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings(api_key="k-secret")),
        patch.object(spec.cls, "_probe_context_tokens", return_value=8192) as probe,
    ):
        window = spec.cls().probe_context_window()
    assert window == 8192
    probe.assert_called_once_with(spec.default_base_url, spec.sample_model, "k-secret")


def test_prelaunch_env_injects_the_probed_window(spec: Spec) -> None:
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings()),
        patch.object(spec.cls, "_probe_context_tokens", return_value=40960),
    ):
        env = spec.cls().prelaunch_model_context_env()
    assert env == {"OPENSCIENTIST_MODEL_CONTEXT_TOKENS": "40960"}


def test_prelaunch_env_forwards_the_operator_pin(spec: Spec) -> None:
    """The pin is set in this process, but the container resolves the window on
    its own side, so returning nothing left it probing and silently taking the
    8192 fallback -- the outcome pinning exists to prevent. Still no probe here."""
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings(model_context_tokens=1234)),
        patch.object(spec.cls, "_probe_context_tokens") as probe,
    ):
        env = spec.cls().prelaunch_model_context_env()
    assert env == {"OPENSCIENTIST_MODEL_CONTEXT_TOKENS": "1234"}
    probe.assert_not_called()


def test_prelaunch_env_is_empty_when_the_probe_fails(spec: Spec) -> None:
    with (
        patch(_BASE_SETTINGS_PATH, return_value=spec.settings()),
        patch.object(spec.cls, "_probe_context_tokens", return_value=None),
    ):
        env = spec.cls().prelaunch_model_context_env()
    assert env == {}

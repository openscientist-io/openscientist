"""Tests for the air-gapped egress allowlist dispatcher.

The dispatcher is tested in isolation over an AirgapPosture, and the per-provider
test pins the allowlist of every proxied provider to the triple.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from openscientist.job_container.egress import (
    AirgapProviderError,
    derive_egress_allowlist,
    format_egress_allowlist,
)
from openscientist.providers import provider_class, provider_ids
from openscientist.providers.base import AirgapEgress, AirgapPosture
from openscientist.settings import ProviderSettings, Settings, clear_settings_cache

BROKER = ("openscientist", 8082)
PROXY = ("openscientist", 8081)


@pytest.fixture(autouse=True)
def _fixed_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openscientist.job_container.egress.container_broker_base_url",
        lambda: "http://openscientist:8082",
    )
    monkeypatch.setattr(
        "openscientist.llm_proxy.container_proxy_base_url",
        lambda: "http://openscientist:8081",
    )


def _settings(database_url: str = "postgresql+asyncpg://u:p@postgres:5432/db") -> Settings:
    return cast(
        Settings,
        SimpleNamespace(database=SimpleNamespace(effective_database_url=database_url)),
    )


def test_proxy_posture_allows_db_broker_and_proxy() -> None:
    entries = derive_egress_allowlist(_settings(), AirgapPosture(AirgapEgress.PROXY))
    assert ("postgres", 5432) in entries
    assert BROKER in entries
    assert PROXY in entries


def test_proxy_posture_excludes_any_provider_endpoint() -> None:
    entries = derive_egress_allowlist(_settings(), AirgapPosture(AirgapEgress.PROXY))
    hosts = {host for host, _ in entries}
    assert hosts == {"postgres", "openscientist"}


#: A fully configured credential set per provider, so each provider reports the
#: posture of a real deployment, not of an incomplete one.
_PROVIDER_ENV: dict[str, dict[str, str]] = {
    "anthropic": {"ANTHROPIC_API_KEY": "sk-test"},
    "cborg": {
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
    },
    "vertex": {
        "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
        "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
        "CLOUD_ML_REGION": "us-east5",
    },
    "bedrock": {
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIA",
        "AWS_SECRET_ACCESS_KEY": "shh",
    },
    "foundry": {"ANTHROPIC_FOUNDRY_RESOURCE": "res", "ANTHROPIC_FOUNDRY_API_KEY": "k"},
    "openai": {"OPENAI_API_KEY": "sk-test"},
    "azure-openai": {"AZURE_OPENAI_API_KEY": "k", "AZURE_OPENAI_RESOURCE": "res"},
    "ollama": {"OLLAMA_BASE_URL": "http://ollama:11434/v1"},
    "vllm": {"VLLM_BASE_URL": "http://vllm:8000/v1"},
    "llamacpp": {"LLAMACPP_BASE_URL": "http://llamacpp:8080/v1"},
}


@pytest.mark.parametrize("provider_id", provider_ids())
def test_air_gapped_allowlist_is_exactly_db_broker_and_proxy_for_every_provider(
    provider_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every provider's air-gapped allow-list is the triple: Postgres, the broker
    and the proxy, nothing else."""
    env = dict(_PROVIDER_ENV[provider_id])
    if provider_id == "vertex":
        creds = tmp_path / "sa.json"
        creds.write_text('{"type": "service_account"}')
        env["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds)
    monkeypatch.delenv("OPENSCIENTIST_MODEL", raising=False)
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", provider_id)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    try:
        posture = provider_class(provider_id).airgap_posture(ProviderSettings(_env_file=None))
    finally:
        clear_settings_cache()
    assert posture.mode is AirgapEgress.PROXY
    entries = derive_egress_allowlist(_settings(), posture)
    assert set(entries) == {("postgres", 5432), BROKER, PROXY}


def test_unsupported_posture_refused() -> None:
    posture = AirgapPosture(AirgapEgress.UNSUPPORTED, reason="Foo cannot be air-gapped.")
    with pytest.raises(AirgapProviderError, match="cannot be air-gapped"):
        derive_egress_allowlist(_settings(), posture)


def test_postgres_default_port_when_absent() -> None:
    entries = derive_egress_allowlist(
        _settings(database_url="postgresql+asyncpg://u:p@db-host/db"),
        AirgapPosture(AirgapEgress.PROXY),
    )
    assert ("db-host", 5432) in entries


def test_deduplicates_entries() -> None:
    # The database host collides with the broker host, so the duplicate drops.
    posture = AirgapPosture(AirgapEgress.PROXY)
    entries = derive_egress_allowlist(
        _settings(database_url="postgresql+asyncpg://u:p@openscientist:8082/db"), posture
    )
    assert entries.count(BROKER) == 1


def test_format_egress_allowlist() -> None:
    rendered = format_egress_allowlist([("postgres", 5432), ("openscientist", 8081)])
    assert rendered == "postgres:5432,openscientist:8081"

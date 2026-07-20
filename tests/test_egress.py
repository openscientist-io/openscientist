"""Tests for the air-gapped egress allowlist derivation."""

from types import SimpleNamespace
from typing import cast

import pytest

from openscientist.job_container.egress import (
    AirgapProviderError,
    derive_egress_allowlist,
    format_egress_allowlist,
)
from openscientist.settings import Settings


@pytest.fixture(autouse=True)
def _fixed_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the broker URL so the allowlist is deterministic regardless of env."""
    monkeypatch.setattr(
        "openscientist.job_container.egress.container_broker_base_url",
        lambda: "http://openscientist:8082",
    )


def _settings(
    *,
    provider_id: str,
    database_url: str = "postgresql+asyncpg://u:p@postgres:5432/db",
    **provider_fields: object,
) -> Settings:
    provider = SimpleNamespace(
        provider_id=provider_id,
        anthropic_base_url=None,
        azure_openai_resource=None,
        anthropic_foundry_base_url=None,
        anthropic_foundry_resource=None,
        ollama_base_url="http://host.docker.internal:11434/v1",
        aws_region=None,
        cloud_ml_region=None,
    )
    for key, value in provider_fields.items():
        setattr(provider, key, value)
    return cast(
        Settings,
        SimpleNamespace(
            provider=provider,
            database=SimpleNamespace(effective_database_url=database_url),
        ),
    )


def test_postgres_and_broker_always_present() -> None:
    entries = derive_egress_allowlist(_settings(provider_id="anthropic"))
    assert ("postgres", 5432) in entries
    assert ("openscientist", 8082) in entries


def test_postgres_default_port_when_absent() -> None:
    entries = derive_egress_allowlist(
        _settings(provider_id="anthropic", database_url="postgresql+asyncpg://u:p@db-host/db")
    )
    assert ("db-host", 5432) in entries


def test_anthropic_default_endpoint() -> None:
    entries = derive_egress_allowlist(_settings(provider_id="anthropic"))
    assert ("api.anthropic.com", 443) in entries


def test_anthropic_custom_base_url() -> None:
    entries = derive_egress_allowlist(
        _settings(provider_id="anthropic", anthropic_base_url="https://llm.internal:8443")
    )
    assert ("llm.internal", 8443) in entries


def test_cborg_uses_base_url() -> None:
    entries = derive_egress_allowlist(
        _settings(provider_id="cborg", anthropic_base_url="https://api.cborg.lbl.gov")
    )
    assert ("api.cborg.lbl.gov", 443) in entries


def test_cborg_without_base_url_refused() -> None:
    with pytest.raises(AirgapProviderError, match="ANTHROPIC_BASE_URL"):
        derive_egress_allowlist(_settings(provider_id="cborg"))


def test_openai_default_endpoint() -> None:
    entries = derive_egress_allowlist(_settings(provider_id="openai"))
    assert ("api.openai.com", 443) in entries


def test_azure_openai_resource_endpoint() -> None:
    entries = derive_egress_allowlist(
        _settings(provider_id="azure-openai", azure_openai_resource="myres")
    )
    assert ("myres.openai.azure.com", 443) in entries


def test_azure_openai_without_resource_refused() -> None:
    with pytest.raises(AirgapProviderError, match="AZURE_OPENAI_RESOURCE"):
        derive_egress_allowlist(_settings(provider_id="azure-openai"))


def test_foundry_base_url_takes_precedence() -> None:
    entries = derive_egress_allowlist(
        _settings(
            provider_id="foundry",
            anthropic_foundry_base_url="https://foundry.example.com/anthropic",
            anthropic_foundry_resource="ignored",
        )
    )
    assert ("foundry.example.com", 443) in entries


def test_foundry_resource_endpoint() -> None:
    entries = derive_egress_allowlist(
        _settings(provider_id="foundry", anthropic_foundry_resource="myfoundry")
    )
    assert ("myfoundry.services.ai.azure.com", 443) in entries


def test_foundry_without_config_refused() -> None:
    with pytest.raises(AirgapProviderError, match="FOUNDRY"):
        derive_egress_allowlist(_settings(provider_id="foundry"))


def test_ollama_from_base_url() -> None:
    entries = derive_egress_allowlist(_settings(provider_id="ollama"))
    assert ("host.docker.internal", 11434) in entries


def test_bedrock_regional_runtime_endpoint() -> None:
    entries = derive_egress_allowlist(_settings(provider_id="bedrock", aws_region="us-east-1"))
    assert ("bedrock-runtime.us-east-1.amazonaws.com", 443) in entries


def test_bedrock_without_region_refused() -> None:
    with pytest.raises(AirgapProviderError, match="AWS_REGION"):
        derive_egress_allowlist(_settings(provider_id="bedrock"))


def test_vertex_regional_and_oauth_endpoints() -> None:
    entries = derive_egress_allowlist(_settings(provider_id="vertex", cloud_ml_region="us-east5"))
    assert ("us-east5-aiplatform.googleapis.com", 443) in entries
    assert ("oauth2.googleapis.com", 443) in entries


def test_vertex_without_region_refused() -> None:
    with pytest.raises(AirgapProviderError, match="CLOUD_ML_REGION"):
        derive_egress_allowlist(_settings(provider_id="vertex"))


def test_unknown_provider_refused() -> None:
    with pytest.raises(AirgapProviderError, match="Unknown provider"):
        derive_egress_allowlist(_settings(provider_id="mystery"))


def test_format_egress_allowlist() -> None:
    rendered = format_egress_allowlist([("postgres", 5432), ("api.anthropic.com", 443)])
    assert rendered == "postgres:5432,api.anthropic.com:443"

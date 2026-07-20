"""Egress allowlist for the air-gapped job-container firewall.

Derives the host:port endpoints the job container may reach when air-gapped:
Postgres, the execution broker, and the active provider's LLM endpoint.
"""

from __future__ import annotations

from urllib.parse import urlparse

from openscientist.exec_broker_client import container_broker_base_url
from openscientist.settings import Settings

_DEFAULT_PORT_BY_SCHEME = {"https": 443, "http": 80}


class AirgapProviderError(ValueError):
    """Raised when the active provider is misconfigured for air-gapped mode."""


def _host_port(url: str, *, default_port: int = 443) -> tuple[str, int]:
    """Parse (host, port) from a URL, defaulting the port from the scheme."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"cannot parse host from URL: {url!r}")
    port = parsed.port or _DEFAULT_PORT_BY_SCHEME.get(parsed.scheme, default_port)
    return host, port


def _provider_endpoints(settings: Settings) -> list[tuple[str, int]]:
    """Return the LLM (host, port) endpoints for the active provider."""
    p = settings.provider
    provider_id = p.provider_id.lower()

    if provider_id == "anthropic":
        return [_host_port(p.anthropic_base_url or "https://api.anthropic.com")]
    if provider_id == "cborg":
        if not p.anthropic_base_url:
            raise AirgapProviderError(
                "ANTHROPIC_BASE_URL is required for cborg in air-gapped mode."
            )
        return [_host_port(p.anthropic_base_url)]
    if provider_id == "openai":
        return [_host_port("https://api.openai.com")]
    if provider_id == "azure-openai":
        if not p.azure_openai_resource:
            raise AirgapProviderError(
                "AZURE_OPENAI_RESOURCE is required for azure-openai in air-gapped mode."
            )
        return [_host_port(f"https://{p.azure_openai_resource}.openai.azure.com")]
    if provider_id == "foundry":
        if p.anthropic_foundry_base_url:
            return [_host_port(p.anthropic_foundry_base_url)]
        if p.anthropic_foundry_resource:
            return [_host_port(f"https://{p.anthropic_foundry_resource}.services.ai.azure.com")]
        raise AirgapProviderError(
            "ANTHROPIC_FOUNDRY_BASE_URL or ANTHROPIC_FOUNDRY_RESOURCE is required "
            "for foundry in air-gapped mode."
        )
    if provider_id == "ollama":
        return [_host_port(p.ollama_base_url, default_port=11434)]
    if provider_id == "bedrock":
        if not p.aws_region:
            raise AirgapProviderError("AWS_REGION is required for bedrock in air-gapped mode.")
        return [_host_port(f"https://bedrock-runtime.{p.aws_region}.amazonaws.com")]
    if provider_id == "vertex":
        if not p.cloud_ml_region:
            raise AirgapProviderError("CLOUD_ML_REGION is required for vertex in air-gapped mode.")
        # Vertex also calls Google's OAuth2 host to mint access tokens.
        return [
            _host_port(f"https://{p.cloud_ml_region}-aiplatform.googleapis.com"),
            _host_port("https://oauth2.googleapis.com"),
        ]

    raise AirgapProviderError(f"Unknown provider {provider_id!r} for air-gapped egress allowlist.")


def derive_egress_allowlist(settings: Settings) -> list[tuple[str, int]]:
    """Return the (host, port) endpoints the air-gapped job container may reach."""
    entries: list[tuple[str, int]] = [
        _host_port(settings.database.effective_database_url, default_port=5432),
        _host_port(container_broker_base_url()),
        *_provider_endpoints(settings),
    ]

    seen: set[tuple[str, int]] = set()
    unique: list[tuple[str, int]] = []
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return unique


def format_egress_allowlist(entries: list[tuple[str, int]]) -> str:
    """Render allowlist entries as the comma-separated host:port env value."""
    return ",".join(f"{host}:{port}" for host, port in entries)

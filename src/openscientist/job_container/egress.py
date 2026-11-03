"""Egress allowlist for the air-gapped job-container firewall.

The allow-list is fixed for every provider: Postgres, the execution broker and
the LLM proxy. An UNSUPPORTED posture is refused.
"""

from __future__ import annotations

from urllib.parse import urlparse

from openscientist.exec_broker_client import container_broker_base_url
from openscientist.providers.base import AirgapEgress, AirgapPosture
from openscientist.settings import Settings

_DEFAULT_PORT_BY_SCHEME = {"https": 443, "http": 80}


class AirgapProviderError(ValueError):
    """Raised when the active provider cannot be air-gapped."""


def _host_port(url: str, *, default_port: int = 443) -> tuple[str, int]:
    """Parse (host, port) from a URL, defaulting the port from the scheme."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"cannot parse host from URL: {url!r}")
    port = parsed.port or _DEFAULT_PORT_BY_SCHEME.get(parsed.scheme, default_port)
    return host, port


def derive_egress_allowlist(settings: Settings, posture: AirgapPosture) -> list[tuple[str, int]]:
    """Return the (host, port) endpoints the air-gapped job container may reach."""
    if posture.mode is AirgapEgress.UNSUPPORTED:
        raise AirgapProviderError(posture.reason or "The active provider cannot be air-gapped.")

    entries: list[tuple[str, int]] = [
        _host_port(settings.database.effective_database_url, default_port=5432),
        _host_port(container_broker_base_url()),
    ]
    if posture.mode is AirgapEgress.PROXY:
        from openscientist.llm_proxy import container_proxy_base_url

        entries.append(_host_port(container_proxy_base_url()))

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

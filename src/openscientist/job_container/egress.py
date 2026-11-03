"""Egress policy for the job-container firewall.

Turns the active provider's air-gap posture (`Provider.airgap_egress`) into the
host:port set the air-gapped container may reach, and lists the web's services
a restricted container keeps when public egress stays open. An UNSUPPORTED
posture is refused.
"""

from __future__ import annotations

from urllib.parse import urlparse

from openscientist.exec_broker_client import container_broker_base_url
from openscientist.providers.base import AirgapEgress, AirgapPosture
from openscientist.settings import Settings

_DEFAULT_PORT_BY_SCHEME = {"https": 443, "http": 80}

#: The firewall entrypoint reads these from the container environment.
FIREWALL_ALLOW_ENV = "OPENSCIENTIST_FIREWALL_ALLOW"
FIREWALL_MODE_ENV = "OPENSCIENTIST_FIREWALL_MODE"
FIREWALL_RUN_AS_ENV = "OPENSCIENTIST_FIREWALL_RUN_AS"

MODE_AIRGAP = "airgap"
MODE_RESTRICT = "restrict"


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


def _unique(entries: list[tuple[str, int]]) -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    unique: list[tuple[str, int]] = []
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return unique


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
    else:
        entries.extend(posture.direct_endpoints)

    return _unique(entries)


def derive_restricted_egress_allowlist(
    settings: Settings, *, include_postgres: bool = True
) -> list[tuple[str, int]]:
    """Return the (host, port) endpoints a public-egress container keeps on the
    private ranges: the web's execution broker and LLM proxy, plus Postgres
    for the agent login when the container logs in."""
    from openscientist.llm_proxy import container_proxy_base_url

    entries: list[tuple[str, int]] = [
        _host_port(container_broker_base_url()),
        _host_port(container_proxy_base_url()),
    ]
    if include_postgres:
        entries.append(_host_port(settings.database.effective_database_url, default_port=5432))

    return _unique(entries)


def format_egress_allowlist(entries: list[tuple[str, int]]) -> str:
    """Render allowlist entries as the comma-separated host:port env value."""
    return ",".join(f"{host}:{port}" for host, port in entries)
